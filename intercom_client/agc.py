"""Automatic level control for speech (a leveller).

Quiet talkers are brought up and loud ones down to one target level, so everybody on the line is about equally loud
whatever the microphone, distance or voice. How it works, per block of microphone audio:

* It measures the block's level (RMS) and tracks a noise floor: the floor drops at once to any lower level and
  rises only slowly, so a pause shows how loud the room is.
* A block counts as speech when it is clearly above that floor (+8 dB) and above a fixed gate. Only speech blocks move
  the gain. In pauses the gain is HELD: the room noise is never pulled up, and the gain does not drift.
* The speech level is smoothed over about half a second; the gain aims at (target level - speech level) within
  [min gain, max gain]. Gain is taken back quickly when the voice gets louder (attack) and given back slowly when it
  gets quieter (release), so it does not pump.
* The very first speech sets the gain right away instead of creeping up for seconds.
* A peak guard acts at once: if a block would exceed `peak_limit` after the gain, the gain is reduced for that block
  already, and then recovers slowly. A soft limiter catches whatever is left.

Gain changes are ramped across each block (no zipper noise).
"""
from __future__ import annotations

import math

import numpy as np

SR = 48000


def _db(x: float) -> float:
    return 20.0 * math.log10(max(x, 1e-9))


def _soft_limit(x: np.ndarray, knee: float = 0.9) -> np.ndarray:
    a = np.abs(x)
    if a.size == 0 or float(a.max()) <= knee:
        return x
    over = np.minimum(np.maximum(a - knee, 0.0) / (1.0 - knee), 20.0)
    return np.where(a > knee, np.sign(x) * (knee + (1.0 - knee) * np.tanh(over)), x).astype(np.float32)


class SpeechLeveller:
    def __init__(self, target_dbfs: float = -20.0, max_gain_db: float = 25.0, min_gain_db: float = -12.0,
                 gate_dbfs: float = -65.0, attack_s: float = 0.05, release_s: float = 0.6, level_s: float = 0.4,
                 peak_limit: float = 0.89):
        self.target_db = target_dbfs
        self.max_gain_db = max_gain_db
        self.min_gain_db = min_gain_db
        self.gate_db = gate_dbfs
        self.attack_s, self.release_s, self.level_s = attack_s, release_s, level_s
        self.peak_limit = peak_limit
        self.gain_db = 0.0  # the gain being applied (readable from any thread)
        self.speaking = False
        self.noise_floor_db = -60.0
        self._level_db: float | None = None
        self._applied = 1.0  # linear gain at the end of the last block

    def _follow(self, desired: float, dt: float) -> None:
        tau = self.attack_s if desired < self.gain_db else self.release_s
        self.gain_db += (1.0 - math.exp(-dt / tau)) * (desired - self.gain_db)

    def process(self, block: np.ndarray) -> np.ndarray:
        n = len(block)
        if n == 0:
            return block
        dt = n / SR
        rms_db = _db(float(np.sqrt(np.mean(np.square(block, dtype=np.float32)))))
        peak = float(np.max(np.abs(block)))

        # noise floor: down at once, up slowly (0.5 dB per second), never above -30 dBFS
        if rms_db < self.noise_floor_db:
            self.noise_floor_db = rms_db
        else:
            self.noise_floor_db = min(self.noise_floor_db + 0.5 * dt, -30.0)

        self.speaking = rms_db > max(self.gate_db, self.noise_floor_db + 8.0)
        if self.speaking:
            if self._level_db is None:
                self._level_db = rms_db
                self.gain_db = min(self.max_gain_db, max(self.min_gain_db, self.target_db - rms_db))  # first speech: at once
            else:
                self._level_db += (1.0 - math.exp(-dt / self.level_s)) * (rms_db - self._level_db)
                desired = min(self.max_gain_db, max(self.min_gain_db, self.target_db - self._level_db))
                self._follow(desired, dt)

        # peak guard: never let this block go over the limit, and stay down afterwards
        guarded = peak * 10.0 ** (self.gain_db / 20.0) > self.peak_limit
        if guarded:
            self.gain_db = _db(self.peak_limit / peak)

        end = 10.0 ** (self.gain_db / 20.0)
        start = min(self._applied, end) if guarded else self._applied  # a guarded block is turned down from its first sample
        ramp = np.linspace(start, end, n, dtype=np.float32) if abs(end - start) > 1e-6 else end
        self._applied = end
        return _soft_limit(block * ramp)
