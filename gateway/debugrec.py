"""Diagnostic recording for one line (config [debug] record_dir): what arrives from the server (packet
sizes/timing), the decoded audio before the playout buffer, and what is handed to the sound card. Used to
tell apart noise that comes from the network/decoder from noise made on the device side.

Audio threads only append to lists (cheap); files are written by finish(), called from the event loop."""
from __future__ import annotations

import csv
import logging
import threading
import time
import wave
from pathlib import Path

import numpy as np

log = logging.getLogger("gateway.debugrec")
SR = 48000


class Recorder:
    def __init__(self, directory: Path, line: str, seconds: float):
        self._dir = directory
        self._stem = f"{''.join(c if c.isalnum() else '_' for c in line)}_{time.strftime('%H%M%S')}"
        self._seconds = seconds
        self._t0 = time.monotonic()
        self._lock = threading.Lock()
        self._rx: list[np.ndarray] = []
        self._rx_t: list[tuple[float, int]] = []
        self._out: list[np.ndarray] = []
        self._packets: list[tuple[float, int, int, int, int, int]] = []
        self.done = False

    def _live(self) -> bool:
        return not self.done and time.monotonic() - self._t0 < self._seconds

    def packet(self, payload_len: int, rtp_ts: int, seq: int, ssrc: int, pt: int) -> None:
        if self._live():
            with self._lock:
                self._packets.append((time.monotonic() - self._t0, seq, rtp_ts, payload_len, ssrc, pt))

    def rx(self, pcm: np.ndarray) -> None:
        if self._live():
            with self._lock:
                self._rx.append(pcm.copy())
                self._rx_t.append((time.monotonic() - self._t0, len(pcm)))

    def out(self, mono: np.ndarray) -> None:
        if self._live():
            with self._lock:
                self._out.append(mono.copy())

    @property
    def full(self) -> bool:
        return not self.done and time.monotonic() - self._t0 >= self._seconds

    def finish(self) -> None:
        if self.done:
            return
        self.done = True
        with self._lock:
            rx, out, packets, rx_t = self._rx, self._out, self._packets, self._rx_t
            self._rx, self._out, self._packets, self._rx_t = [], [], [], []
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            for name, chunks in (("rx", rx), ("out", out)):
                if chunks:
                    pcm = (np.clip(np.concatenate(chunks), -1, 1) * 32767).astype(np.int16)
                    with wave.open(str(self._dir / f"{self._stem}_{name}.wav"), "wb") as w:
                        w.setnchannels(1)
                        w.setsampwidth(2)
                        w.setframerate(SR)
                        w.writeframes(pcm.tobytes())
            with (self._dir / f"{self._stem}_packets.csv").open("w", newline="") as f:
                wr = csv.writer(f)
                wr.writerow(["t_s", "seq", "rtp_ts", "payload_bytes", "ssrc", "pt"])
                wr.writerows((round(t, 4), *rest) for t, *rest in packets)
            with (self._dir / f"{self._stem}_rxframes.csv").open("w", newline="") as f:
                wr = csv.writer(f)
                wr.writerow(["t_s", "samples"])
                wr.writerows((round(t, 4), n) for t, n in rx_t)
            log.info("diagnostic recording written to %s (%s_*)", self._dir, self._stem)
        except Exception:
            log.exception("writing the diagnostic recording failed")
