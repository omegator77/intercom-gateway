"""The per-SSRC receive pipeline and the multi-talker playout, without any network."""
import asyncio
import types

import numpy as np
import pytest
from aiortc.codecs.opus import OpusEncoder
from aiortc.rtp import RtpPacket
import av

import gateway.audio as audio
from gateway.audio import MixedPlayout, SR, tone_amplitude_near
from gateway.rtc import LineConnection


def opus_packets(freq: float, seconds: float = 1.0, amp: float = 0.3) -> list[bytes]:
    enc = OpusEncoder()
    out = []
    n = int(seconds * 50)
    for i in range(n):
        t = (np.arange(960) + i * 960) / SR
        pcm = (amp * np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)
        frame = av.AudioFrame.from_ndarray(np.repeat(pcm, 2).reshape(1, -1), format="s16", layout="stereo")
        frame.sample_rate = SR
        frame.pts = i * 960
        payloads, _ = enc.encode(frame)
        out += payloads
    return out


class FakeReceiver:
    def __init__(self):
        codec = types.SimpleNamespace(mimeType="audio/opus", clockRate=48000)
        self._RTCRtpReceiver__codecs = {111: codec, 0: types.SimpleNamespace(mimeType="audio/PCMU", clockRate=8000)}
        self._RTCRtpReceiver__remote_streams = {}


def rtp(payload: bytes, seq: int, ssrc: int, pt: int = 111) -> RtpPacket:
    return RtpPacket(payload_type=pt, sequence_number=seq & 0xFFFF, timestamp=seq * 960, ssrc=ssrc, payload=payload)


@pytest.fixture
def conn():
    got: list[tuple[np.ndarray, int]] = []

    async def make():
        c = LineConnection(None, "tok", None, lambda pcm, key: got.append((pcm, key)))
        return c

    c = asyncio.run(make())
    return c, got


def feed(c, rx, payloads, ssrc, start_seq=100):
    for i, p in enumerate(payloads):
        c._on_rtp(rx, rtp(p, start_seq + i, ssrc))


def test_two_ssrcs_are_decoded_separately(conn):
    c, got = conn
    rx = FakeReceiver()
    feed(c, rx, opus_packets(440), ssrc=11)
    feed(c, rx, opus_packets(880), ssrc=22)
    for key, hz, other in ((11, 440, 880), (22, 880, 440)):
        pcm = np.concatenate([p for p, k in got if k == key])
        assert len(pcm) >= SR * 0.95
        assert tone_amplitude_near(pcm[SR // 5:], hz) > 0.2
        assert tone_amplitude_near(pcm[SR // 5:], other) < 0.01  # no cross-talk between streams


def test_duplicates_and_late_packets_are_dropped_and_gaps_counted(conn):
    c, got = conn
    rx = FakeReceiver()
    pk = opus_packets(440, 0.5)
    c._on_rtp(rx, rtp(pk[0], 1, 5))
    c._on_rtp(rx, rtp(pk[1], 2, 5))
    c._on_rtp(rx, rtp(pk[1], 2, 5))  # duplicate
    c._on_rtp(rx, rtp(pk[4], 5, 5))  # 3, 4 missing
    c._on_rtp(rx, rtp(pk[2], 3, 5))  # late
    assert len(got) == 3
    assert c.rx_lost == 2


def test_non_opus_and_empty_payloads_are_ignored(conn):
    c, got = conn
    rx = FakeReceiver()
    c._on_rtp(rx, rtp(b"\x00" * 160, 1, 7, pt=0))
    c._on_rtp(rx, rtp(b"", 2, 7))
    c._on_rtp(rx, rtp(b"\x00" * 5, 3, 7, pt=99))  # unknown payload type
    assert got == []


def test_garbage_opus_payload_does_not_raise(conn):
    c, got = conn
    c._on_rtp(FakeReceiver(), rtp(b"\xff" * 3, 1, 9))  # must be swallowed (it would kill aiortc's receive loop)


def test_connection_has_the_session_methods():
    for name in ("connect", "heartbeat", "close"):
        assert callable(getattr(LineConnection, name))


# ---------------------------------------------------------------- MixedPlayout

@pytest.fixture
def clock(monkeypatch):
    class C:
        t = 0.0

        def __call__(self):
            return self.t

    c = C()
    monkeypatch.setattr(audio.time, "monotonic", c)
    return c


def test_playout_sums_talkers_and_drops_idle_streams(clock):
    mp = MixedPlayout(target_ms=60)
    for _ in range(6):
        mp.write(np.full(960, 0.1, np.float32), key=1)
        mp.write(np.full(960, 0.2, np.float32), key=2)
    out = mp.read(480)
    assert mp.streams == 2
    assert np.allclose(out, 0.3, atol=1e-3)
    clock.t = 10.0
    mp.read(480)
    assert mp.streams == 0  # both went quiet long ago


def test_end_of_a_talker_is_not_counted_as_underrun(clock):
    mp = MixedPlayout(target_ms=60)
    for _ in range(5):
        mp.write(np.full(960, 0.1, np.float32), key=1)
    for tick in range(60):
        clock.t = tick * 0.01
        mp.read(480)
    clock.t = 5.0
    mp.read(480)
    assert mp.underruns == 0
