import numpy as np
import pytest

import gateway.audio as audio
from gateway.audio import DriftBuffer, SR


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(audio.time, "monotonic", c)
    return c


def simulate(clock, ppm=0.0, seconds=120, jitter_ms=0.0, target_ms=60, seed=1):
    """Producer: 20 ms frames of a sine on a clock that is `ppm` fast. Consumer: 10 ms blocks on the
    nominal clock. Returns (buffer, fill history in ms after warm-up, output samples)."""
    rng = np.random.default_rng(seed)
    buf = DriftBuffer(target_ms=target_ms)
    k = 1.0 + ppm * 1e-6
    n_frames = int(seconds / 0.02 * k) + 10
    arrivals = np.arange(n_frames) * 0.02 / k
    if jitter_ms:
        arrivals = np.maximum.accumulate(arrivals + rng.uniform(0, jitter_ms / 1000.0, n_frames))
    phase = np.arange(n_frames * 960)
    sine = (0.5 * np.sin(2 * np.pi * 1000 * phase / SR)).astype(np.float32)
    fills, outs, nxt = [], [], 0
    for tick in range(int(seconds / 0.01)):
        clock.t = tick * 0.01
        while nxt < n_frames and arrivals[nxt] <= clock.t:
            buf.write(sine[nxt * 960:(nxt + 1) * 960])
            nxt += 1
        outs.append(buf.read(480))
        if clock.t > 20:
            fills.append(buf.fill_ms)
    return buf, np.array(fills), np.concatenate(outs)


@pytest.mark.parametrize("ppm", [0, 100, -100, 400, -400])
def test_clock_drift_is_absorbed_without_glitches(clock, ppm):
    buf, fills, out = simulate(clock, ppm=ppm)
    assert buf.underruns == 0
    assert buf.overruns == 0
    assert fills.min() > 20 and fills.max() < 110, (fills.min(), fills.max())
    assert abs(buf.ratio - 1.0) <= 0.005
    steady = out[SR * 25:]
    assert np.max(np.abs(np.diff(steady))) < 0.2  # a 1 kHz sine steps ~0.065 per sample: no clicks


def test_packet_jitter_is_absorbed(clock):
    buf, fills, _ = simulate(clock, ppm=50, jitter_ms=15)
    assert buf.underruns == 0 and buf.overruns == 0
    assert fills.min() > 10


def test_silent_line_is_not_counted_as_underrun(clock):
    buf = DriftBuffer(target_ms=60)
    for _ in range(50):  # one second of audio, delivered at once
        buf.write(np.full(960, 0.1, np.float32))
    for tick in range(400):  # 4 s: audio runs out after ~1 s, then silence (nobody talking)
        clock.t = tick * 0.01
        buf.read(480)
    assert buf.underruns == 0
    clock.t = 4.0
    buf.write(np.full(960, 0.1, np.float32))  # speech resumes
    assert not np.any(buf.read(480))  # prefetching again
    for _ in range(3):
        clock.t += 0.02
        buf.write(np.full(960, 0.1, np.float32))
    assert np.any(buf.read(480))


def test_real_starvation_counts_and_recovers(clock):
    buf = DriftBuffer(target_ms=60)
    for _ in range(5):
        buf.write(np.full(960, 0.1, np.float32))
    for _ in range(12):  # consume more than was delivered, while data is still "recent"
        buf.read(480)
    assert buf.underruns == 1
    for _ in range(4):
        buf.write(np.full(960, 0.1, np.float32))
    assert np.any(buf.read(480))


def test_backlog_after_a_burst_is_worked_off_quickly(clock):
    """A stalled event loop delivers a burst: the buffer sits ~220 ms above target. It must drain back
    in well under 30 s without any glitch (the first version needed ~40 s)."""
    buf = DriftBuffer(target_ms=60)
    for _ in range(14):  # 280 ms delivered at once, below the overrun limit
        buf.write(np.full(960, 0.1, np.float32))
    pending = 0.0
    for tick in range(3000):  # 30 s of perfectly steady 20 ms frames afterwards
        clock.t = tick * 0.01
        pending += 0.5
        while pending >= 1.0:
            buf.write(np.full(960, 0.1, np.float32))
            pending -= 1.0
        buf.read(480)
    assert buf.underruns == 0 and buf.overruns == 0
    assert 30 < buf.fill_ms < 90, buf.fill_ms


def test_overrun_jumps_back_to_target(clock):
    buf = DriftBuffer(target_ms=60, max_ms=300)
    for _ in range(30):  # 600 ms with nobody reading
        buf.write(np.zeros(960, np.float32))
    assert buf.overruns >= 1
    assert buf.fill_ms <= 300
