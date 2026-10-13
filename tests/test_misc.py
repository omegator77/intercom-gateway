import asyncio

import numpy as np
import pytest

from gateway.audio import DeviceError, DeviceInputTrack, FRAME, match_device
from gateway.rtc import strip_extra_fingerprints

APIS = [{"name": "MME"}, {"name": "Windows WASAPI"}]


def dev(name, api, ins=0, outs=0):
    return {"name": name, "hostapi": api, "max_input_channels": ins, "max_output_channels": outs}


DEVICES = [
    dev("DVS Receive  1-2 (Dante Virtua", 0, ins=2),
    dev("DVS Receive  1-2 (Dante Virtual Soundcard)", 1, ins=2),
    dev("DVS Receive 11-12 (Dante Virtual Soundcard)", 1, ins=2),
    dev("DVS Transmit  1-2 (Dante Virtual Soundcard)", 1, outs=2),
    dev("Lautsprecher (Realtek(R) Audio)", 1, outs=2),
    dev("Mikrofon (A)", 1, ins=1),
    dev("Mikrofon (B)", 1, ins=1),
]


def test_match_prefers_wasapi_and_normalises_whitespace():
    assert match_device(DEVICES, APIS, "DVS Receive 1-2", "input") == 1
    assert match_device(DEVICES, APIS, "dvs   transmit 1-2", "output") == 3


def test_match_does_not_confuse_1_2_with_11_12():
    assert match_device(DEVICES, APIS, "Receive 11-12", "input") == 2


def test_match_respects_direction_and_refuses_guessing():
    with pytest.raises(DeviceError, match="no input"):
        match_device(DEVICES, APIS, "DVS Transmit 1-2", "input")
    with pytest.raises(DeviceError, match="ambiguous"):
        match_device(DEVICES, APIS, "Mikrofon", "input")
    with pytest.raises(DeviceError, match="no output"):
        match_device(DEVICES, APIS, "nonexistent", "output")


def test_sdp_keeps_only_sha256_fingerprint():
    sdp = "\r\n".join([
        "v=0", "m=audio 9 UDP/TLS/RTP/SAVPF 111",
        "a=fingerprint:sha-256 AA:BB", "a=fingerprint:sha-384 CC:DD", "a=fingerprint:sha-512 EE:FF",
        "a=setup:passive", "",
    ])
    out = strip_extra_fingerprints(sdp)
    assert "sha-256" in out and "sha-384" not in out and "sha-512" not in out
    assert out.endswith("\r\n") and "a=setup:passive" in out


def test_track_returns_exact_20ms_frames_from_odd_chunk_sizes():
    async def go():
        track = DeviceInputTrack(asyncio.get_running_loop())
        ramp = (np.arange(FRAME * 3) % 1000 / 1000.0 - 0.5).astype(np.float32)
        for i in range(0, len(ramp), 480):
            track.push(ramp[i:i + 480])
        got = []
        for _ in range(3):
            f = await asyncio.wait_for(track.recv(), 1)
            assert f.samples == FRAME and f.sample_rate == 48000
            got.append(f.to_ndarray().reshape(-1))
        expected = (ramp * 32767).astype(np.int16)
        assert np.array_equal(np.concatenate(got), expected)
    asyncio.run(go())


def test_track_keeps_rtp_alive_with_silence_when_the_device_stalls():
    async def go():
        track = DeviceInputTrack(asyncio.get_running_loop())
        frames = [await asyncio.wait_for(track.recv(), 1) for _ in range(5)]  # nothing pushed at all
        assert track.stalls == 1  # one 100 ms wait covers this frame plus four owed ones
        assert all(not np.any(f.to_ndarray()) for f in frames)
        assert [f.pts for f in frames] == [i * FRAME for i in range(5)]
    asyncio.run(go())


def test_track_drops_oldest_audio_when_consumer_falls_behind():
    async def go():
        track = DeviceInputTrack(asyncio.get_running_loop(), max_buffer_ms=100)
        for _ in range(20):  # 200 ms pushed, only 100 ms allowed
            track.push(np.zeros(480, np.float32))
        assert track.dropped_samples > 0
    asyncio.run(go())
