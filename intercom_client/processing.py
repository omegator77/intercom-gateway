"""Microphone processing: echo cancellation, noise suppression and automatic gain.

This wraps the WebRTC audio processing module (the same family of algorithms the browser uses; PyPI package
`aec-audio-processing`). Echo cancellation needs to know what is being played to the speakers (the "reverse
stream"): the hub hands in every output block, and the module works out the delay between playing a sound and
hearing it in the microphone by itself.

Everything works in 10 ms frames of 48 kHz mono. Blocks of other sizes are cut into frames with a small buffer.
"""
from __future__ import annotations

import threading

import numpy as np

SR = 48000
FRAME = 480  # 10 ms


class ProcessingUnavailable(Exception):
    """The audio processing library is not installed or could not be loaded."""


def _to_bytes(x: np.ndarray) -> bytes:
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def _from_bytes(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype="<i2").astype(np.float32) / 32767.0


class MicProcessor:
    def __init__(self, echo_cancellation: bool, noise_suppression: int, auto_gain: bool):
        if not (echo_cancellation or noise_suppression or auto_gain):
            raise ValueError("nothing to process")  # the module returns silence when everything is off
        try:
            from aec_audio_processing import AudioProcessor
        except Exception as e:
            raise ProcessingUnavailable(
                f"Echo-/Rauschunterdrückung nicht verfügbar ({type(e).__name__}: {e}). "
                "Installieren mit: pip install aec-audio-processing") from e
        self.echo, self.noise, self.agc = echo_cancellation, noise_suppression, auto_gain
        self.needs_reverse = echo_cancellation
        self._ap = AudioProcessor(enable_aec=echo_cancellation, enable_ns=noise_suppression > 0,
                                  ns_level=max(noise_suppression - 1, 0), enable_agc=auto_gain, agc_mode=1,
                                  enable_vad=False)
        self._ap.set_stream_format(SR, 1, SR, 1)
        self._ap.set_reverse_stream_format(SR, 1)
        self._lock = threading.Lock()  # the microphone and the playback run on different audio threads
        self._mic_in = np.zeros(0, np.float32)
        self._mic_out = np.zeros(0, np.float32)
        self._ref_in = np.zeros(0, np.float32)

    def process(self, block: np.ndarray) -> np.ndarray:
        """One block from the microphone -> the same number of processed samples."""
        n = len(block)
        with self._lock:
            self._mic_in = np.concatenate([self._mic_in, block.astype(np.float32, copy=False)])
            while len(self._mic_in) >= FRAME:
                frame, self._mic_in = self._mic_in[:FRAME], self._mic_in[FRAME:]
                self._mic_out = np.concatenate([self._mic_out, _from_bytes(self._ap.process_stream(_to_bytes(frame)))])
            if len(self._mic_out) >= n:
                out, self._mic_out = self._mic_out[:n], self._mic_out[n:]
            else:  # only while the buffers fill up after an unusual block size
                out = np.concatenate([self._mic_out, np.zeros(n - len(self._mic_out), np.float32)])
                self._mic_out = np.zeros(0, np.float32)
        return out

    def reverse(self, block: np.ndarray) -> None:
        """One block that is being played to the speakers (what the echo cancellation has to remove)."""
        if not self.needs_reverse:
            return
        with self._lock:
            self._ref_in = np.concatenate([self._ref_in, block.astype(np.float32, copy=False)])
            while len(self._ref_in) >= FRAME:
                frame, self._ref_in = self._ref_in[:FRAME], self._ref_in[FRAME:]
                self._ap.process_reverse_stream(_to_bytes(frame))
