"""The intercom side of a call: each line a call uses is joined by a normal gateway `LineWorker`, whose audio
backend is not a sound card but this binding. A line can be used in one direction only (a "Kommentar" line is
only listened to, an "N-1" line is only spoken into)."""
from __future__ import annotations

import asyncio

import numpy as np

from gateway.audio import DeviceInputTrack, Level, MixedPlayout, db_to_lin
from gateway.config import AudioCfg, LineCfg


class LineBinding:
    """Persistent handle on one line for the whole life of a call. The worker re-creates its audio object after
    every reconnect; the binding keeps pointing at whichever one is current, and is silent in between."""

    def __init__(self, name: str, listens: bool, talks: bool, in_gain_db: float = 0.0, out_gain_db: float = 0.0):
        self.name = name
        self.listens = listens  # the call sends this line's sound to the other side
        self.talks = talks  # the call speaks into this line
        self.audio: SipLineAudio | None = None
        self._to_line = db_to_lin(in_gain_db)  # from the call into the line
        self._from_line = db_to_lin(out_gain_db)  # from the line to the call

    @property
    def connected(self) -> bool:
        return self.audio is not None

    def pull(self, n: int) -> np.ndarray:
        """`n` samples (48 kHz mono) of what the line sounds like right now; silence if it is not connected."""
        a = self.audio
        if not self.listens or a is None:
            return np.zeros(n, np.float32)
        block = a.out_buf.read(n) * self._from_line
        a.out_level.update(block)
        return block

    def push(self, mono: np.ndarray) -> None:
        a = self.audio
        if self.talks and a is not None and len(mono):
            a.track.push(mono * self._to_line if self._to_line != 1.0 else mono)


class SipLineAudio:
    """Implements the gateway's `LineAudio` interface on top of a `LineBinding`."""

    def __init__(self, binding: LineBinding, cfg: LineCfg, audio: AudioCfg, loop: asyncio.AbstractEventLoop):
        self.binding = binding
        self.cfg = cfg
        self.track = DeviceInputTrack(loop)  # even a line we only listen to needs it: it keeps the stream alive with silence
        self.out_buf = MixedPlayout(target_ms=cfg.playout_ms or audio.playout_ms)
        self.out_level = Level()
        self.recorder = None

    def on_remote_pcm(self, mono: np.ndarray, key: int = 0) -> None:
        if self.binding.listens:
            self.out_buf.write(mono, key)

    async def start(self) -> None:
        self.binding.audio = self

    async def stop(self) -> None:
        if self.binding.audio is self:
            self.binding.audio = None
        self.track.stop()

    def levels(self) -> tuple[float, float]:
        return self.track.level.dbfs, self.out_level.dbfs

    def stats(self) -> dict:
        return {
            "talkers": self.out_buf.streams,
            "fill_ms": round(self.out_buf.fill_ms, 1),
            "underruns": self.out_buf.underruns,
            "overruns": self.out_buf.overruns,
            "in_dropped_samples": self.track.dropped_samples,
            "in_stalls": self.track.stalls,
        }
