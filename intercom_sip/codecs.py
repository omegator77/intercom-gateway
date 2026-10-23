"""The audio codecs of a SIP call and the conversion between them and the intercom's 48 kHz float PCM.

Everything is built on libav (PyAV, which aiortc brings along anyway): MPEG-1/2 Layer II ("mp2", the mandatory
broadcast codec of EBU Tech 3326), G.722, G.711 and 16 bit linear PCM. AAC-LD needs the Fraunhofer encoder, which
the stock libav builds do not contain; it is deliberately not offered here (see the README).

Callers hand over / get back `(n, channels)` float32 arrays at 48 kHz; resampling to the codec's own rate is done
by libav.
"""
from __future__ import annotations

import fractions
from dataclasses import dataclass

import av
import numpy as np

SR = 48000


class CodecError(Exception):
    pass


@dataclass(frozen=True)
class CodecDef:
    key: str  # name in the config
    rtp_name: str  # encoding name in SDP
    static_pt: int | None  # payload type fixed by RFC 3551, None = dynamic
    clock: int | None  # RTP clock rate; None = the sample rate (linear PCM)
    sample_rates: tuple[int, ...]
    channels: tuple[int, ...]
    ff_name: str  # libav codec name
    frame_based: bool  # one encoder frame per RTP packet (mp2); otherwise a byte stream cut into ptime pieces
    bytes_per_ms: float | None = None  # constant bit rate codecs: bytes per millisecond (None = depends on the format)


CODECS: dict[str, CodecDef] = {c.key: c for c in (
    CodecDef("mp2", "MPA", 14, 90000, (32000, 44100, 48000), (1, 2), "mp2", True),
    CodecDef("g722", "G722", 9, 8000, (16000,), (1,), "g722", False, 8.0),  # RTP clock 8000 is historic, audio is 16 kHz
    CodecDef("pcmu", "PCMU", 0, 8000, (8000,), (1,), "pcm_mulaw", False, 8.0),
    CodecDef("pcma", "PCMA", 8, 8000, (8000,), (1,), "pcm_alaw", False, 8.0),
    CodecDef("l16", "L16", None, None, (32000, 44100, 48000), (1, 2), "pcm_s16be", False),
)}
BY_RTP_NAME = {c.rtp_name.upper(): c for c in CODECS.values()}

# Layer II bit rates (kbit/s) that exist for a mono stream and for stereo / dual channel (ISO 11172-3 table)
MP2_MONO_KBPS = (32, 48, 56, 64, 80, 96, 112, 128, 160, 192)
MP2_STEREO_KBPS = (64, 96, 112, 128, 160, 192, 224, 256, 320, 384)
MODES = ("mono", "stereo", "dual-mono")
MP2_MODE_BITS = {"stereo": 0b00, "dual-mono": 0b10, "mono": 0b11}  # bits 7-6 of the fourth header byte
MP2_SAMPLES_PER_FRAME = 1152


@dataclass(frozen=True)
class AudioFormat:
    codec: str  # key of CODECS
    sample_rate: int
    channels: int  # 1 or 2
    bitrate_kbps: int = 0  # mp2 only
    mode: str = "stereo"  # "mono", "stereo" or "dual-mono" (the last two only differ in how a Layer II stream is flagged)
    ptime_ms: int = 20  # constant bit rate codecs: audio per RTP packet

    @property
    def spec(self) -> CodecDef:
        return CODECS[self.codec]

    @property
    def clock(self) -> int:
        return self.spec.clock or self.sample_rate

    def describe(self) -> str:
        if self.codec == "mp2":
            return f"MPEG Layer II {self.bitrate_kbps} kbit/s {self.sample_rate / 1000:g} kHz {self.mode}"
        names = {"g722": "G.722", "pcmu": "G.711 u-law", "pcma": "G.711 A-law"}
        if self.codec == "l16":
            return f"linear 16 bit {self.sample_rate / 1000:g} kHz {'stereo' if self.channels == 2 else 'mono'}"
        return names[self.codec]


def validate_format(f: AudioFormat) -> AudioFormat:
    """Raise CodecError unless this combination exists; returns `f`."""
    spec = CODECS.get(f.codec)
    if spec is None:
        raise CodecError(f"unknown codec {f.codec!r} (known: {', '.join(CODECS)})")
    if f.sample_rate not in spec.sample_rates:
        raise CodecError(f"{f.codec} cannot run at {f.sample_rate} Hz (allowed: {list(spec.sample_rates)})")
    if f.channels not in spec.channels:
        raise CodecError(f"{f.codec} cannot carry {f.channels} channel(s)")
    if f.mode not in MODES:
        raise CodecError(f"mode must be one of {MODES}")
    if f.codec == "mp2" and (f.mode == "mono") != (f.channels == 1):
        raise CodecError(f"mode {f.mode!r} does not match {f.channels} channel(s)")
    if f.codec != "mp2" and f.mode == "dual-mono":
        raise CodecError("dual-mono exists for MPEG Layer II only; use stereo for the other codecs")
    if f.codec == "mp2":
        allowed = MP2_MONO_KBPS if f.channels == 1 else MP2_STEREO_KBPS
        if f.bitrate_kbps not in allowed:
            raise CodecError(f"Layer II {f.mode} has no {f.bitrate_kbps} kbit/s (allowed: {list(allowed)})")
    if f.ptime_ms not in (10, 20, 30, 40, 60):
        raise CodecError("ptime_ms must be 10, 20, 30, 40 or 60")
    return f


def layout_name(channels: int) -> str:
    return "mono" if channels == 1 else "stereo"


def _to_s16(pcm: np.ndarray, channels: int) -> np.ndarray:
    """(n, c) or (n,) float32 -> packed int16 row of shape (1, n * channels)."""
    if pcm.ndim == 1:
        pcm = pcm[:, None]
    if pcm.shape[1] != channels:
        pcm = np.repeat(pcm[:, :1], channels, axis=1) if pcm.shape[1] == 1 else pcm[:, :channels]
    return (np.clip(pcm, -1.0, 1.0) * 32767.0).astype(np.int16).reshape(1, -1)


class Encoder:
    """48 kHz float PCM in, a list of encoded chunks out. For Layer II every chunk is exactly one frame; for the
    other codecs the chunks are arbitrary pieces of one byte stream (the packetizer cuts it by ptime)."""

    def __init__(self, fmt: AudioFormat):
        self.fmt = validate_format(fmt)
        ctx = av.CodecContext.create(fmt.spec.ff_name, "w")
        ctx.sample_rate = fmt.sample_rate
        ctx.layout = layout_name(fmt.channels)
        ctx.format = "s16"
        if fmt.codec == "mp2":
            ctx.bit_rate = fmt.bitrate_kbps * 1000
        ctx.open()
        self._ctx = ctx
        self._pts = 0

    def encode(self, pcm: np.ndarray) -> list[bytes]:
        """pcm: (n, channels) float32 at 48 kHz."""
        n = len(pcm)
        if n == 0:
            return []
        frame = av.AudioFrame.from_ndarray(_to_s16(pcm, self.fmt.channels), format="s16",
                                           layout=layout_name(self.fmt.channels))
        frame.sample_rate = SR
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, SR)
        self._pts += n
        out = []
        for packet in self._ctx.encode(frame):
            data = bytes(packet)
            if self.fmt.codec == "mp2":
                data = flag_mp2_mode(data, self.fmt.mode)
            out.append(data)
        return out


def flag_mp2_mode(frame: bytes, mode: str) -> bytes:
    """Set the channel mode in the header of one Layer II frame. libav's encoder writes "stereo" for two channels;
    "dual channel" (two independent programmes) is coded identically and differs only in these two header bits
    (no CRC is written, so nothing else depends on them). Anything that is not a Layer II frame is left alone."""
    if len(frame) < 4 or frame[0] != 0xFF or (frame[1] & 0xE0) != 0xE0 or (frame[1] >> 1) & 0x03 != 0b10:
        return frame
    bits = MP2_MODE_BITS[mode]
    if (frame[3] >> 6) == 0b11 or mode == "mono":  # a mono frame stays mono
        return frame
    return frame[:3] + bytes([(frame[3] & 0x3F) | (bits << 6)]) + frame[4:]


class Decoder:
    """Encoded payload in, 48 kHz float PCM out, whatever rate and channel count the stream actually has."""

    def __init__(self, fmt: AudioFormat):
        self.fmt = validate_format(fmt)
        ctx = av.CodecContext.create(fmt.spec.ff_name, "r")
        if fmt.codec != "mp2":  # the stream carries no description of itself
            ctx.sample_rate = fmt.sample_rate
            ctx.layout = layout_name(fmt.channels)
            ctx.format = "s16"
        ctx.open()
        self._ctx = ctx
        self._resampler: av.AudioResampler | None = None
        self._resampler_key: tuple | None = None
        self.errors = 0
        self.last_stream: tuple[int, int] | None = None  # (sample rate, channels) of the last decoded frame

    def decode(self, data: bytes) -> np.ndarray | None:
        """Returns (n, channels) float32 at 48 kHz, or None if the data did not complete a frame."""
        if not data:
            return None
        try:
            packets = self._ctx.parse(data) if self.fmt.codec == "mp2" else [av.Packet(data)]
            frames = []
            for p in packets:
                frames.extend(self._ctx.decode(p))
        except av.error.FFmpegError:
            self.errors += 1
            return None
        parts = [self._convert(f) for f in frames]
        parts = [p for p in parts if p is not None and len(p)]
        if not parts:
            return None
        return np.concatenate(parts) if len(parts) > 1 else parts[0]

    def _convert(self, frame: av.AudioFrame) -> np.ndarray | None:
        ch = len(frame.layout.channels)
        key = (frame.format.name, frame.layout.name, frame.sample_rate)
        if key != self._resampler_key:
            self._resampler = av.AudioResampler(format="flt", layout=layout_name(min(ch, 2)), rate=SR)
            self._resampler_key = key
        self.last_stream = (frame.sample_rate, ch)
        out = []
        for f in self._resampler.resample(frame):
            a = f.to_ndarray()
            out.append(a.reshape(-1, min(ch, 2)))
        return np.concatenate(out) if out else None
