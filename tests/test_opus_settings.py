"""Stereo sending and the per-line Opus mode / bitrate."""
import asyncio

import numpy as np
import pytest
from aiortc.codecs.opus import OpusDecoder
from aiortc.jitterbuffer import JitterFrame

from gateway import opus
from gateway.audio import FRAME, DeviceInputTrack
from gateway.config import ConfigError, parse_config

BASE_LINE = {"name": "PGM", "production_id": 2, "line_id": 1, "username": "gw", "password": "x",
             "input_device": "DVS Receive 1-2"}


def line(**over):
    cfg = parse_config({"server": {"url": "https://example.test"}, "audio": {"backend": "portaudio"},
                        "line": [{**BASE_LINE, **over}]}, env={})
    return cfg.lines[0]


# ---------------------------------------------------------------- config

def test_defaults_keep_the_old_behaviour():
    c = line()
    assert (c.stereo, c.opus_mode, c.opus_bitrate_kbps) == (False, "voip", 96)
    assert c.input_channels == (0,)


def test_stereo_defaults_to_the_first_two_channels_and_needs_exactly_two():
    assert line(stereo=True).input_channels == (0, 1)
    assert line(stereo=True, input_channels=[2, 3]).input_channels == (2, 3)
    with pytest.raises(ConfigError, match="exactly two"):
        line(stereo=True, input_channels=[0])
    with pytest.raises(ConfigError, match="exactly two"):
        line(stereo=True, input_channels=[0, 1, 2])
    with pytest.raises(ConfigError, match="stereo must be"):
        line(stereo="yes")


def test_opus_mode_and_bitrate_are_validated():
    c = line(opus_mode="audio", opus_bitrate_kbps=128)
    assert (c.opus_mode, c.opus_bitrate_kbps) == ("audio", 128)
    with pytest.raises(ConfigError, match="opus_mode"):
        line(opus_mode="music")
    for bad in (8, 300, "fast", None):
        with pytest.raises(ConfigError, match="opus_bitrate_kbps"):
            line(opus_bitrate_kbps=bad)


# ---------------------------------------------------------------- stereo track

def test_stereo_track_sends_interleaved_left_and_right():
    async def go():
        track = DeviceInputTrack(asyncio.get_running_loop(), channels=2)
        left = np.full(FRAME, 0.25, np.float32)
        right = np.full(FRAME, -0.5, np.float32)
        track.push(np.stack([left, right], axis=1))
        f = await asyncio.wait_for(track.recv(), 1)
        assert f.layout.name == "stereo" and f.samples == FRAME
        pcm = f.to_ndarray().reshape(-1, 2)  # packed s16: L R L R ...
        assert np.all(pcm[:, 0] == int(0.25 * 32767)) and np.all(pcm[:, 1] == int(-0.5 * 32767))
    asyncio.run(go())


def test_stereo_track_copies_a_mono_chunk_to_both_channels_and_keeps_silence_stereo():
    async def go():
        track = DeviceInputTrack(asyncio.get_running_loop(), channels=2)
        track.push(np.full(FRAME, 0.1, np.float32))
        pcm = (await asyncio.wait_for(track.recv(), 1)).to_ndarray().reshape(-1, 2)
        assert np.array_equal(pcm[:, 0], pcm[:, 1]) and pcm[0, 0] != 0
        silent = await asyncio.wait_for(track.recv(), 1)  # nothing pushed: keep-alive silence must be stereo too
        assert silent.layout.name == "stereo" and not np.any(silent.to_ndarray())
    asyncio.run(go())


def test_mono_track_mixes_a_stereo_chunk_down():
    async def go():
        track = DeviceInputTrack(asyncio.get_running_loop())
        track.push(np.stack([np.full(FRAME, 0.2, np.float32), np.full(FRAME, 0.4, np.float32)], axis=1))
        f = await asyncio.wait_for(track.recv(), 1)
        assert f.layout.name == "mono"
        assert abs(int(f.to_ndarray()[0, 0]) - int(0.3 * 32767)) <= 1
    asyncio.run(go())


# ---------------------------------------------------------------- encoder

def _encode(enc, seconds=2.0, stereo=True):
    """Encodes a music-like test signal; returns the list of Opus packets."""
    import av

    n = int(48000 * seconds)
    t = np.arange(n) / 48000
    rng = np.random.default_rng(1)
    left = 0.3 * np.sin(2 * np.pi * 440 * t) + 0.05 * rng.standard_normal(n)
    right = 0.3 * np.sin(2 * np.pi * 660 * t) + 0.05 * rng.standard_normal(n)
    pcm = (np.clip(np.stack([left, right], axis=1), -1, 1) * 32767).astype(np.int16)
    packets = []
    for i in range(0, n - FRAME + 1, FRAME):
        frame = av.AudioFrame.from_ndarray(pcm[i:i + FRAME].reshape(1, -1), format="s16", layout="stereo")
        frame.sample_rate = 48000
        frame.pts = i
        frame.time_base = __import__("fractions").Fraction(1, 48000)
        payloads, _ = enc.encode(frame)
        packets += payloads
    return packets


def test_bitrate_setting_changes_the_size_of_the_stream():
    low = sum(map(len, _encode(opus.ConfiguredOpusEncoder("audio", 32))))
    high = sum(map(len, _encode(opus.ConfiguredOpusEncoder("audio", 128))))
    assert high > 2.5 * low
    # 128 kbit/s for 2 s is about 32 kB (allow for the encoder's rate control)
    assert 25_000 < high < 40_000


def test_stereo_survives_the_opus_round_trip():
    packets = _encode(opus.ConfiguredOpusEncoder("audio", 128))
    dec = OpusDecoder()
    out = []
    for p in packets[5:60]:  # skip the encoder's start-up
        (frame,) = dec.decode(JitterFrame(data=p, timestamp=0))
        assert frame.layout.name == "stereo"
        out.append(frame.to_ndarray().reshape(-1, 2).astype(np.float32))
    pcm = np.concatenate(out)
    # left carries 440 Hz, right 660 Hz: each channel must keep its own tone
    def dominant(x):
        spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
        return np.argmax(spec) * 48000 / len(x)
    assert abs(dominant(pcm[:, 0]) - 440) < 15
    assert abs(dominant(pcm[:, 1]) - 660) < 15


def test_encoder_gets_the_requested_mode_and_bitrate():
    enc = opus.ConfiguredOpusEncoder("audio", 128)
    assert (enc.mode, enc.bitrate_kbps) == ("audio", 128)
    assert enc.codec.bit_rate == 128_000
    assert enc.codec.options["application"] == "audio"  # (libopus reads this when it opens on the first frame)


def test_the_mode_really_reaches_libopus():
    # At 32 kbit/s libopus codes this signal in a speech-leaning way for "voip" and switches to its music
    # mode (TOC config 31, CELT) for "audio". Same signal, same bitrate: only the application differs.
    def configs(mode):
        return {p[0] >> 3 for p in _encode(opus.ConfiguredOpusEncoder(mode, 32), seconds=1.0)[3:]}

    assert 31 in configs("audio") and 31 not in configs("voip")


# ---------------------------------------------------------------- install on an aiortc sender

class FakeSender:
    _RTCRtpSender__encoder = None


def test_install_replaces_the_encoder_only_for_non_default_settings():
    s = FakeSender()
    assert opus.install(s, "voip", 96) is False and s._RTCRtpSender__encoder is None
    assert opus.install(s, "audio", 96) is True
    assert isinstance(s._RTCRtpSender__encoder, opus.ConfiguredOpusEncoder)
    s2 = FakeSender()
    assert opus.install(s2, "voip", 128) is True and s2._RTCRtpSender__encoder.codec.bit_rate == 128_000


def test_install_does_not_crash_when_aiortc_has_no_such_attribute(caplog):
    assert opus.install(object(), "audio", 128) is False
    assert "ignored" in caplog.text


def test_a_real_aiortc_sender_has_the_attribute_we_rely_on():
    # guards against an aiortc upgrade that renames it: then the setting would silently do nothing
    from aiortc import RTCPeerConnection

    async def go():
        pc = RTCPeerConnection()
        sender = pc.addTrack(DeviceInputTrack(asyncio.get_running_loop()))
        assert opus.install(sender, "audio", 128) is True
        assert isinstance(sender._RTCRtpSender__encoder, opus.ConfiguredOpusEncoder)
        await pc.close()
    asyncio.run(go())


# ---------------------------------------------------------------- stereo playback, limiter, per-line buffer

from gateway.audio import DriftBuffer, MixedPlayout, SR, soft_limit


def test_config_stereo_output_limiter_and_line_playout():
    c = line()
    assert (c.stereo_output, c.limiter, c.playout_ms) == (False, True, None)
    c = line(stereo_output=True, output_channels=[2, 3], limiter=False, playout_ms=120)
    assert (c.stereo_output, c.output_channels, c.limiter, c.playout_ms) == (True, (2, 3), False, 120)
    with pytest.raises(ConfigError, match="exactly two output_channels"):
        line(stereo_output=True, output_channels=[0])
    with pytest.raises(ConfigError, match="limiter must be"):
        line(limiter="on")
    for bad in (10, 900, "x"):
        with pytest.raises(ConfigError, match="playout_ms"):
            line(playout_ms=bad)


def test_soft_limiter_is_transparent_below_the_knee_and_never_exceeds_full_scale():
    quiet = np.linspace(-0.8, 0.8, 1000).astype(np.float32)
    assert np.array_equal(soft_limit(quiet), quiet)
    loud = np.linspace(-6.0, 6.0, 4001).astype(np.float32)
    out = soft_limit(loud)
    assert np.all(np.abs(out) <= 1.0) and out.dtype == np.float32
    assert np.all(np.diff(out) >= 0)  # monotonic: no folding back
    assert abs(soft_limit(np.array([0.85], np.float32))[0] - 0.85) < 0.02  # gentle just above the knee
    assert soft_limit(np.array([np.inf, -np.inf], np.float32)).tolist() == [1.0, -1.0]
    assert soft_limit(np.zeros(0, np.float32)).size == 0


def test_stereo_drift_buffer_keeps_left_and_right_apart():
    b = DriftBuffer(target_ms=20, channels=2)
    left = np.full(SR // 4, 0.3, np.float32)
    right = np.full(SR // 4, -0.6, np.float32)
    b.write(np.stack([left, right], axis=1))
    out = b.read(2000)
    assert out.shape == (2000, 2)
    assert np.allclose(out[:, 0], 0.3, atol=1e-4) and np.allclose(out[:, 1], -0.6, atol=1e-4)


def test_stereo_buffer_centres_a_mono_talker_and_mono_buffer_mixes_stereo_down():
    s = DriftBuffer(target_ms=20, channels=2)
    s.write(np.full(SR // 4, 0.2, np.float32))
    out = s.read(1000)
    assert np.allclose(out[:, 0], 0.2, atol=1e-4) and np.allclose(out[:, 1], 0.2, atol=1e-4)
    m = DriftBuffer(target_ms=20)
    m.write(np.stack([np.full(SR // 4, 0.2, np.float32), np.full(SR // 4, 0.6, np.float32)], axis=1))
    assert np.allclose(m.read(1000), 0.4, atol=1e-4)


def test_mixed_playout_sums_stereo_talkers_per_channel_and_scales_its_limit_with_the_buffer():
    mp = MixedPlayout(target_ms=400, channels=2)
    mp.write(np.stack([np.full(SR * 9 // 20, 0.1, np.float32), np.zeros(SR * 9 // 20, np.float32)], axis=1), key=1)
    mp.write(np.stack([np.full(SR * 9 // 20, 0.2, np.float32), np.full(SR * 9 // 20, 0.5, np.float32)], axis=1), key=2)
    out = mp.read(1000)
    assert out.shape == (1000, 2)
    assert np.allclose(out[:, 0], 0.3, atol=1e-4) and np.allclose(out[:, 1], 0.5, atol=1e-4)
    assert mp.streams == 2 and mp.overruns == 0  # a 400 ms target fits: the old fixed 300 ms ceiling would overrun


def test_each_line_can_have_its_own_playout_buffer():
    from gateway.audio import SyntheticLineAudio
    from gateway.config import AudioCfg

    async def go():
        loop = asyncio.get_running_loop()
        default = SyntheticLineAudio(line(), AudioCfg("portaudio", 60), loop)
        custom = SyntheticLineAudio(line(playout_ms=150), AudioCfg("portaudio", 60), loop)
        assert default.out_buf._target_ms == 60 and custom.out_buf._target_ms == 150
    asyncio.run(go())
