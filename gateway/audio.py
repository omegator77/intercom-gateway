"""Audio plumbing: drift-compensated playout buffer, device-paced send track, backends.

Two backends share one interface (LineAudio):
  * PortAudioLineAudio  - real devices via sounddevice (WASAPI), one capture + one playback stream per line
  * SyntheticLineAudio  - test tones in / analyzer out, no sound card (used by the e2e tests)
"""
from __future__ import annotations

import asyncio
import collections
import fractions
import logging
import math
import re
import threading
import time
from typing import Protocol

import av
import numpy as np
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack

from .config import AudioCfg, LineCfg

log = logging.getLogger("gateway.audio")

SR = 48000
FRAME = 960  # 20 ms, one Opus frame


class DeviceError(Exception):
    pass


def db_to_lin(db: float) -> float:
    return 10.0 ** (db / 20.0)


class Level:
    """Smoothed RMS level in dBFS (for status output and later UI meters)."""

    def __init__(self) -> None:
        self._rms = 0.0

    def update(self, x: np.ndarray) -> None:
        if x.size:
            self._rms = 0.7 * self._rms + 0.3 * float(np.sqrt(np.mean(np.square(x, dtype=np.float32))))

    @property
    def dbfs(self) -> float:
        return 20.0 * math.log10(max(self._rms, 1e-6))


def tone_amplitude(x: np.ndarray, freq: float) -> float:
    """Amplitude (0..1 full scale) of a sine at `freq` via a single-bin DFT with a Hann window."""
    n = len(x)
    if n < SR // 10:
        return 0.0
    w = np.hanning(n)
    c = np.exp(-2j * np.pi * freq * np.arange(n) / SR)
    return float(2 * abs(np.dot(x.astype(np.float64) * w, c)) / np.sum(w))


def tone_amplitude_near(x: np.ndarray, freq: float, tolerance: float = 0.012) -> float:
    """Like tone_amplitude, but searches +-`tolerance` around `freq`: the playout resampler may play
    up to 0.5 % (temporarily 2 %) fast or slow, which moves a pure tone out of the exact DFT bin."""
    n = len(x)
    if n < SR // 10:
        return 0.0
    w = np.hanning(n)
    spectrum = np.abs(np.fft.rfft(x.astype(np.float64) * w))  # one FFT instead of many single-bin DFTs
    bins = np.fft.rfftfreq(n, 1.0 / SR)
    band = (bins >= freq * (1.0 - tolerance)) & (bins <= freq * (1.0 + tolerance))
    return float(2 * spectrum[band].max() / np.sum(w)) if band.any() else 0.0


class DriftBuffer:
    """Playout buffer between the network (frames arrive on the sender's clock) and the sound
    card (blocks are pulled on the device clock).

    The two clocks never match exactly (tens of ppm), so a fixed buffer slowly drains or overflows.
    read() therefore resamples with a ratio that is nudged by the slow-moving buffer fill
    (linear interpolation, +-0.5 % normally, up to +-2 % while far above target, time constant
    ~10 s) - no clicks from dropping or repeating samples. A prefetch (target_ms) absorbs jitter.
    """

    def __init__(self, sr: int = SR, target_ms: int = 60, max_ms: int = 300, capacity_s: float = 2.0):
        self._sr = sr
        self._cap = int(sr * capacity_s)
        self._buf = np.zeros(self._cap, np.float32)
        self._w = 0  # absolute number of samples written
        self._r = 0.0  # absolute (fractional) read position
        self._target = sr * target_ms / 1000.0
        self._max = sr * max_ms / 1000.0
        self._buffering = True
        self._fill_avg: float | None = None
        self._last_write = 0.0
        self._lock = threading.Lock()
        self.underruns = 0  # starvation while data was flowing (not counted when the line is simply silent)
        self.overruns = 0
        self.ratio = 1.0

    @property
    def fill_ms(self) -> float:
        with self._lock:
            return (self._w - self._r) * 1000.0 / self._sr

    def write(self, x: np.ndarray) -> None:
        n = len(x)
        if n == 0:
            return
        with self._lock:
            if n >= self._cap:
                x = x[-(self._cap - 1):]
                n = len(x)
            self._buf[(np.arange(n) + self._w) % self._cap] = x
            self._w += n
            self._last_write = time.monotonic()
            if self._w - self._r > self._max:  # consumer stalled or clock far off: jump back to target
                self._r = self._w - self._target
                self.overruns += 1

    def read(self, n: int) -> np.ndarray:
        out = np.zeros(n, np.float32)
        with self._lock:
            fill = self._w - self._r
            if self._buffering:
                if fill < self._target:
                    return out
                self._buffering = False
                self._fill_avg = fill

            alpha = min(1.0, n / self._sr)  # ~1 s smoothing of the fill level
            self._fill_avg = fill if self._fill_avg is None else self._fill_avg + (fill - self._fill_avg) * alpha
            err_s = (self._fill_avg - self._target) / self._sr
            # +-0.5 % is inaudible and plenty for clock drift (tens of ppm). After a burst (a stalled
            # loop, a network hiccup) the buffer can sit far above target; then allow up to +-2 % so
            # the extra latency is worked off in seconds instead of half a minute.
            limit = 0.02 if abs(err_s) > 0.1 else 0.005
            ratio = 1.0 + max(-limit, min(limit, 0.1 * err_s))

            if self._r + ratio * (n - 1) + 1 >= self._w:  # not enough data for a full block
                if time.monotonic() - self._last_write < 0.2:
                    self.underruns += 1  # data was flowing and ran dry -> a real glitch
                self._buffering = True
                self._fill_avg = None
                return out

            pos = self._r + ratio * np.arange(n)
            i0 = np.floor(pos).astype(np.int64)
            frac = (pos - i0).astype(np.float32)
            a = self._buf[i0 % self._cap]
            b = self._buf[(i0 + 1) % self._cap]
            out = a + (b - a) * frac
            self._r += ratio * n
            self.ratio = ratio
            return out


class DeviceInputTrack(MediaStreamTrack):
    """Outgoing WebRTC audio, paced by whoever calls push() (the capture device's clock).

    recv() returns exactly one 20 ms frame as soon as enough samples are buffered. If the source
    stalls (device unplugged) it keeps the RTP stream alive with silence - the media bridge drops
    endpoints that go quiet for 60 s.
    """

    kind = "audio"

    def __init__(self, loop: asyncio.AbstractEventLoop, max_buffer_ms: int = 200):
        super().__init__()
        self._loop = loop
        self._chunks: collections.deque[np.ndarray] = collections.deque()
        self._avail = 0
        self._max = int(SR * max_buffer_ms / 1000)
        self._lock = threading.Lock()
        self._event = asyncio.Event()
        self._n = 0
        self._owed_silence = 0
        self.level = Level()
        self.dropped_samples = 0
        self.stalls = 0

    def push(self, mono: np.ndarray) -> None:
        """Called from the audio thread (or the loop thread). `mono` is float32 in -1..1."""
        with self._lock:
            self._chunks.append(np.array(mono, dtype=np.float32, copy=True))
            self._avail += len(mono)
            while self._avail > self._max and len(self._chunks) > 1:  # consumer behind: drop oldest
                dropped = self._chunks.popleft()
                self._avail -= len(dropped)
                self.dropped_samples += len(dropped)
            ready = self._avail >= FRAME
        if ready:
            try:
                self._loop.call_soon_threadsafe(self._event.set)
            except RuntimeError:  # loop already closed during shutdown
                pass

    def _take(self) -> np.ndarray | None:
        with self._lock:
            if self._avail < FRAME:
                return None
            parts: list[np.ndarray] = []
            need = FRAME
            while need > 0:
                c = self._chunks[0]
                if len(c) <= need:
                    parts.append(c)
                    need -= len(c)
                    self._chunks.popleft()
                else:
                    parts.append(c[:need])
                    self._chunks[0] = c[need:]
                    need = 0
            self._avail -= FRAME
            return np.concatenate(parts)

    async def recv(self) -> av.AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError
        samples: np.ndarray | None = None
        if self._owed_silence > 0:
            self._owed_silence -= 1
            samples = np.zeros(FRAME, np.float32)
        while samples is None:
            self._event.clear()
            samples = self._take()
            if samples is not None:
                break
            try:
                await asyncio.wait_for(self._event.wait(), 0.1)
            except asyncio.TimeoutError:
                self.stalls += 1
                self._owed_silence = 4  # 100 ms elapsed = this frame + 4 more
                samples = np.zeros(FRAME, np.float32)
        self.level.update(samples)
        pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)
        frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = SR
        frame.pts = self._n * FRAME
        frame.time_base = fractions.Fraction(1, SR)
        self._n += 1
        return frame


class LineAudio(Protocol):
    track: DeviceInputTrack

    def on_remote_pcm(self, mono: np.ndarray) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def levels(self) -> tuple[float, float]: ...
    def stats(self) -> dict: ...


# ---------------------------------------------------------------- device lookup

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def match_device(devices: list[dict], hostapis: list[dict], query: str, kind: str,
                 prefer: str = "Windows WASAPI") -> int:
    """Find the device index whose (whitespace-normalised) name contains `query`.

    WASAPI is preferred because the legacy host APIs truncate names to 31 characters. Raises
    DeviceError when nothing matches or when the query is ambiguous - guessing which physical
    channel pair to open is exactly the kind of mistake worth refusing."""
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    q = _norm(query)
    cands = [(i, d) for i, d in enumerate(devices) if d[key] > 0 and q in _norm(d["name"])]
    if not cands:
        raise DeviceError(f"no {kind} device matching {query!r}")
    preferred = [c for c in cands if hostapis[c[1]["hostapi"]]["name"] == prefer]
    chosen = preferred or cands
    names = {_norm(d["name"]) for _, d in chosen}
    if len(names) > 1:
        raise DeviceError(f"{kind} device {query!r} is ambiguous: {sorted(names)}")
    return chosen[0][0]


# ---------------------------------------------------------------- backends

class _BaseLineAudio:
    def __init__(self, cfg: LineCfg, audio: AudioCfg, loop: asyncio.AbstractEventLoop):
        self.cfg = cfg
        self.track = DeviceInputTrack(loop)
        self.out_buf = DriftBuffer(target_ms=audio.playout_ms)
        self.out_level = Level()
        self._in_gain = db_to_lin(cfg.in_gain_db)
        self._out_gain = db_to_lin(cfg.out_gain_db)

    def on_remote_pcm(self, mono: np.ndarray) -> None:
        self.out_buf.write(mono)

    def levels(self) -> tuple[float, float]:
        return self.track.level.dbfs, self.out_level.dbfs

    def stats(self) -> dict:
        return {
            "fill_ms": round(self.out_buf.fill_ms, 1),
            "ratio": round(self.out_buf.ratio, 5),
            "underruns": self.out_buf.underruns,
            "overruns": self.out_buf.overruns,
            "in_dropped_samples": self.track.dropped_samples,
            "in_stalls": self.track.stalls,
        }


class PortAudioLineAudio(_BaseLineAudio):
    """One capture stream (device -> track) and one playback stream (buffer -> device) per line."""

    def __init__(self, cfg: LineCfg, audio: AudioCfg, loop: asyncio.AbstractEventLoop):
        super().__init__(cfg, audio, loop)
        self._in = None
        self._out = None
        self._cb_error_logged = False

    async def start(self) -> None:
        import sounddevice as sd  # imported lazily: tests and the synthetic backend need no PortAudio

        devices = sd.query_devices()
        apis = sd.query_hostapis()
        cfg = self.cfg
        if cfg.input_device:
            idx = match_device(devices, apis, cfg.input_device, "input")
            ch = int(devices[idx]["max_input_channels"])
            if max(cfg.input_channels) >= ch:
                raise DeviceError(f"{devices[idx]['name']!r} has {ch} input channels, config wants {cfg.input_channels}")
            chans = list(cfg.input_channels)

            def in_cb(indata, frames, time_info, status):
                try:
                    mono = indata[:, chans[0]] if len(chans) == 1 else indata[:, chans].mean(axis=1)
                    self.track.push(mono * self._in_gain)
                except Exception:  # never let an exception kill the audio thread
                    self._log_cb_error("capture")

            self._in = sd.InputStream(device=idx, samplerate=SR, channels=ch, dtype="float32",
                                      blocksize=480, callback=in_cb)
            log.info("[%s] capture  <- %s (channels %s)", cfg.name, devices[idx]["name"], chans)
        if cfg.output_device:
            idx = match_device(devices, apis, cfg.output_device, "output")
            ch = int(devices[idx]["max_output_channels"])
            if max(cfg.output_channels) >= ch:
                raise DeviceError(f"{devices[idx]['name']!r} has {ch} output channels, config wants {cfg.output_channels}")
            ochans = list(cfg.output_channels)

            def out_cb(outdata, frames, time_info, status):
                try:
                    mono = np.clip(self.out_buf.read(frames) * self._out_gain, -1.0, 1.0)
                    outdata.fill(0)
                    for c in ochans:
                        outdata[:, c] = mono
                    self.out_level.update(mono)
                except Exception:
                    outdata.fill(0)
                    self._log_cb_error("playback")

            self._out = sd.OutputStream(device=idx, samplerate=SR, channels=ch, dtype="float32",
                                        blocksize=480, callback=out_cb)
            log.info("[%s] playback -> %s (channels %s)", cfg.name, devices[idx]["name"], ochans)
        try:
            for s in (self._in, self._out):
                if s is not None:
                    s.start()
        except Exception:
            await self.stop()
            raise

    def _log_cb_error(self, which: str) -> None:
        if not self._cb_error_logged:  # once: this runs on the audio thread, 100x per second
            self._cb_error_logged = True
            log.exception("[%s] %s callback failed", self.cfg.name, which)

    async def stop(self) -> None:
        for s in (self._in, self._out):
            if s is not None:
                try:
                    s.stop()
                    s.close()
                except Exception:
                    log.exception("[%s] closing audio stream", self.cfg.name)
        self._in = self._out = None
        self.track.stop()


class SyntheticLineAudio(_BaseLineAudio):
    """No sound card: sends a tone (or silence) at wall-clock pace and records what comes back."""

    def __init__(self, cfg: LineCfg, audio: AudioCfg, loop: asyncio.AbstractEventLoop):
        super().__init__(cfg, audio, loop)
        self.tone_hz: float | None = cfg.tone_hz
        self.history: collections.deque[tuple[float, np.ndarray]] = collections.deque(maxlen=6000)
        self._tasks: list[asyncio.Task] = []
        self._phase = 0

    async def start(self) -> None:
        self._tasks = [asyncio.ensure_future(self._generate()), asyncio.ensure_future(self._play())]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self.track.stop()

    async def _generate(self) -> None:
        t0 = time.monotonic()
        n = 0
        while True:
            delay = t0 + n * 0.01 - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if self.tone_hz:
                t = (np.arange(480) + self._phase) / SR
                x = (0.25 * np.sin(2 * np.pi * self.tone_hz * t)).astype(np.float32)
                self._phase += 480
            else:
                x = np.zeros(480, np.float32)
            self.track.push(x * self._in_gain)
            n += 1

    async def _play(self) -> None:
        t0 = time.monotonic()
        n = 0
        while True:
            delay = t0 + n * 0.01 - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            x = np.clip(self.out_buf.read(480) * self._out_gain, -1.0, 1.0)
            self.out_level.update(x)
            self.history.append((time.monotonic(), x))
            n += 1

    def heard(self, t0: float, t1: float) -> np.ndarray:
        parts = [x for t, x in self.history if t0 <= t < t1]
        return np.concatenate(parts) if parts else np.zeros(0, np.float32)


def make_line_audio(cfg: LineCfg, audio: AudioCfg, loop: asyncio.AbstractEventLoop) -> LineAudio:
    if audio.backend == "synthetic":
        return SyntheticLineAudio(cfg, audio, loop)
    return PortAudioLineAudio(cfg, audio, loop)
