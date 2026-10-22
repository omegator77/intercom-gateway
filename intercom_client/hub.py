"""One microphone and one output shared by all lines.

The gateway gives every line its own sound card channels; a person has one headset. The hub captures the
microphone once and hands each line either the microphone (while that line is being talked into) or silence,
and mixes what all lines receive into the one output, each with its own volume and mute.

`process_mic` and `mix_output` are the whole audio logic; the two backends (PortAudio and a synthetic one for
tests) only move blocks of 10 ms in and out of them.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from gateway.audio import DeviceError, DeviceInputTrack, Level, MixedPlayout, SR, db_to_lin, match_device, soft_limit

from .config import ClientAudio
from .talk import TalkControl

log = logging.getLogger("client.hub")

BLOCK = 480  # 10 ms at 48 kHz


@dataclass
class LineControl:
    """Everything the user can set on one line. Lives as long as the app, so it survives a reconnect."""
    name: str
    volume_pct: int = 100  # 0..150
    listen_mute: bool = False
    talk: TalkControl = field(default_factory=TalkControl)

    @property
    def gain(self) -> float:
        return 0.0 if self.listen_mute else self.volume_pct / 100.0


class SharedLineAudio:
    """The audio side of one connected line (what gateway.session.LineWorker expects), backed by the hub."""

    def __init__(self, control: LineControl, hub: "AudioHub", loop: asyncio.AbstractEventLoop, playout_ms: int):
        self.control = control
        self._hub = hub
        self.track = DeviceInputTrack(loop)
        self.out_buf = MixedPlayout(target_ms=playout_ms)
        self.out_level = Level()
        self.recorder = None  # the worker may set one for diagnostics; unused here

    def on_remote_pcm(self, mono: np.ndarray, key: int = 0) -> None:
        self.out_buf.write(mono, key)

    async def start(self) -> None:
        self._hub.attach(self)

    async def stop(self) -> None:
        self._hub.detach(self)
        self.track.stop()

    def levels(self) -> tuple[float, float]:
        return self.track.level.dbfs, self.out_level.dbfs

    def stats(self) -> dict:
        return {
            "talkers": self.out_buf.streams,
            "fill_ms": round(self.out_buf.fill_ms, 1),
            "ratio": round(self.out_buf.ratio, 5),
            "underruns": self.out_buf.underruns,
            "overruns": self.out_buf.overruns,
            "in_dropped_samples": self.track.dropped_samples,
            "in_stalls": self.track.stalls,
        }


class AudioHub:
    def __init__(self, cfg: ClientAudio):
        self.cfg = cfg
        self.global_mute = False  # nobody is transmitting, whatever the lines say
        self.mic_level = Level()
        self.out_level = Level()
        self._mic_gain = db_to_lin(cfg.mic_gain_db)
        self._out_gain = db_to_lin(cfg.output_gain_db)
        self._lines: tuple[SharedLineAudio, ...] = ()
        self._lock = threading.Lock()
        self._silence = np.zeros(SR, np.float32)
        self._cb_error_logged = False

    # -- lines come and go while the audio keeps running

    def attach(self, line: SharedLineAudio) -> None:
        with self._lock:
            self._lines = (*[x for x in self._lines if x.control.name != line.control.name], line)

    def detach(self, line: SharedLineAudio) -> None:
        with self._lock:
            self._lines = tuple(x for x in self._lines if x is not line)

    # -- the audio logic (called every 10 ms from the audio threads)

    def process_mic(self, block: np.ndarray) -> None:
        """One block from the microphone: to every line that is being talked into, silence to the others."""
        mic = soft_limit(block * self._mic_gain) if self._mic_gain != 1.0 else block
        self.mic_level.update(mic)
        silence = self._silence[: len(mic)]
        muted = self.global_mute
        for line in self._lines:
            line.track.push(mic if (line.control.talk.transmitting and not muted) else silence)

    def mix_output(self, frames: int) -> np.ndarray:
        """The next `frames` samples for the output: every line at its own volume, summed and limited."""
        out = np.zeros(frames, np.float32)
        for line in self._lines:
            x = line.out_buf.read(frames)  # also for muted lines: their buffers must keep draining
            y = x * line.control.gain
            line.out_level.update(y)
            if line.control.gain > 0:
                out += y
        out = soft_limit(out * self._out_gain) if self._out_gain != 1.0 else soft_limit(out)
        self.out_level.update(out)
        return out

    def _log_cb_error(self, which: str) -> None:
        if not self._cb_error_logged:  # this runs on the audio thread, 100x per second
            self._cb_error_logged = True
            log.exception("%s callback failed", which)

    async def start(self) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError


def _extra_settings(sd, device: dict, hostapis: list[dict]):
    """WASAPI shared mode only runs at the device's own rate unless auto-convert is on; the client works at 48 kHz."""
    if hostapis[device["hostapi"]]["name"] == "Windows WASAPI" and hasattr(sd, "WasapiSettings"):
        try:
            return sd.WasapiSettings(auto_convert=True)
        except TypeError:  # an older sounddevice without the option
            return None
    return None


class PortAudioHub(AudioHub):
    def __init__(self, cfg: ClientAudio):
        super().__init__(cfg)
        self._in = None
        self._out = None

    async def start(self) -> None:
        import sounddevice as sd  # lazily: tests and the synthetic backend need no PortAudio

        devices, apis = sd.query_devices(), sd.query_hostapis()
        cfg = self.cfg
        try:
            in_idx = (match_device(devices, apis, cfg.input_device, "input") if cfg.input_device
                      else sd.default.device[0])
            out_idx = (match_device(devices, apis, cfg.output_device, "output") if cfg.output_device
                       else sd.default.device[1])
        except DeviceError:
            raise
        if in_idx is None or in_idx < 0:
            raise DeviceError("no microphone found - choose one in the settings")
        if out_idx is None or out_idx < 0:
            raise DeviceError("no audio output found - choose one in the settings")
        din, dout = devices[in_idx], devices[out_idx]
        if din["max_input_channels"] <= cfg.input_channel:
            raise DeviceError(f"{din['name']!r} has {din['max_input_channels']} input channel(s), "
                              f"channel {cfg.input_channel + 1} was asked for")
        out_channels = [c for c in cfg.output_channels if c < dout["max_output_channels"]] or [0]

        def in_cb(indata, frames, time_info, status):
            try:
                self.process_mic(indata[:, cfg.input_channel])
            except Exception:
                self._log_cb_error("microphone")

        def out_cb(outdata, frames, time_info, status):
            try:
                mono = self.mix_output(frames)
                outdata.fill(0)
                for c in out_channels:
                    outdata[:, c] = mono
            except Exception:
                outdata.fill(0)
                self._log_cb_error("playback")

        try:
            self._in = sd.InputStream(device=in_idx, samplerate=SR, channels=cfg.input_channel + 1, dtype="float32",
                                      blocksize=BLOCK, callback=in_cb, extra_settings=_extra_settings(sd, din, apis))
            self._out = sd.OutputStream(device=out_idx, samplerate=SR, channels=max(out_channels) + 1,
                                        dtype="float32", blocksize=BLOCK, callback=out_cb,
                                        extra_settings=_extra_settings(sd, dout, apis))
            self._in.start()
            self._out.start()
        except Exception as e:
            await self.stop()
            raise DeviceError(f"could not open the audio devices: {e}") from e
        log.info("microphone <- %s, output -> %s", din["name"], dout["name"])

    async def stop(self) -> None:
        for s in (self._in, self._out):
            if s is not None:
                try:
                    s.stop()
                    s.close()
                except Exception:
                    log.exception("closing an audio stream")
        self._in = self._out = None


class SyntheticHub(AudioHub):
    """No sound card: the "microphone" is a tone (or silence), the output is recorded. For tests."""

    def __init__(self, cfg: ClientAudio, tone_hz: float | None = 440.0):
        super().__init__(cfg)
        self.tone_hz = tone_hz
        self.history: list[tuple[float, np.ndarray]] = []
        self._tasks: list[asyncio.Task] = []
        self._phase = 0

    async def start(self) -> None:
        self._tasks = [asyncio.ensure_future(self._mic()), asyncio.ensure_future(self._speaker())]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def _paced(self, step) -> None:
        t0, n = time.monotonic(), 0
        while True:
            delay = t0 + n * 0.01 - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            step()
            n += 1

    async def _mic(self) -> None:
        def step() -> None:
            if self.tone_hz:
                t = (np.arange(BLOCK) + self._phase) / SR
                self._phase += BLOCK
                self.process_mic((0.25 * np.sin(2 * np.pi * self.tone_hz * t)).astype(np.float32))
            else:
                self.process_mic(np.zeros(BLOCK, np.float32))

        await self._paced(step)

    async def _speaker(self) -> None:
        def step() -> None:
            self.history.append((time.monotonic(), self.mix_output(BLOCK)))
            del self.history[:-6000]

        await self._paced(step)

    def heard(self, t0: float, t1: float) -> np.ndarray:
        parts = [x for t, x in self.history if t0 <= t < t1]
        return np.concatenate(parts) if parts else np.zeros(0, np.float32)


def make_hub(cfg: ClientAudio) -> AudioHub:
    return SyntheticHub(cfg) if cfg.backend == "synthetic" else PortAudioHub(cfg)
