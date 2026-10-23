"""RTP (RFC 3550) packets and the payload formats of the supported codecs.

Layer II uses the MPEG audio payload of RFC 2250 / 3551 (a 4 byte header in front of every packet, a frame that
does not fit one packet is fragmented), the others are plain byte streams cut into `ptime` pieces.
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass

from .codecs import AudioFormat, MP2_SAMPLES_PER_FRAME

MTU_PAYLOAD = 1200  # stays clear of the usual 1500 byte path MTU, with IP, UDP, RTP and tunnel overhead


@dataclass(frozen=True)
class RtpPacket:
    pt: int
    seq: int
    ts: int
    ssrc: int
    marker: bool
    payload: bytes


def build_rtp(pt: int, seq: int, ts: int, ssrc: int, payload: bytes, marker: bool = False) -> bytes:
    return struct.pack("!BBHII", 0x80, (0x80 if marker else 0) | (pt & 0x7F), seq & 0xFFFF, ts & 0xFFFFFFFF,
                       ssrc & 0xFFFFFFFF) + payload


def parse_rtp(data: bytes) -> RtpPacket | None:
    """None for anything that is not a plain RTP version 2 packet (RTCP, STUN, garbage)."""
    if len(data) < 12 or data[0] >> 6 != 2:
        return None
    b0, b1, seq, ts, ssrc = struct.unpack("!BBHII", data[:12])
    if 192 <= b1 <= 223:  # RTCP shares the port in some setups; its packet types look like marker + payload type 64..95 (RFC 5761)
        return None
    offset = 12 + 4 * (b0 & 0x0F)  # CSRC list
    if b0 & 0x10:  # header extension
        if len(data) < offset + 4:
            return None
        offset += 4 + 4 * struct.unpack("!H", data[offset + 2:offset + 4])[0]
    end = len(data)
    if b0 & 0x20 and end > offset:  # padding
        end -= data[-1]
    if offset > end:
        return None
    return RtpPacket(b1 & 0x7F, seq, ts, ssrc, bool(b1 & 0x80), data[offset:end])


class Packetizer:
    """Encoded chunks in, RTP datagrams out. Sequence number, timestamp and SSRC start at random values."""

    def __init__(self, fmt: AudioFormat, pt: int):
        self.fmt = fmt
        self.pt = pt
        self.ssrc = int.from_bytes(os.urandom(4), "big")
        self.seq = int.from_bytes(os.urandom(2), "big")
        self._ts0 = int.from_bytes(os.urandom(4), "big")
        self._samples = 0  # audio samples (at the codec's own rate) sent so far; the timestamp follows from it
        self._buf = bytearray()
        spec = fmt.spec
        if spec.frame_based:
            self._unit = 0
        else:
            per_ms = spec.bytes_per_ms if spec.bytes_per_ms else fmt.sample_rate * 2 * fmt.channels / 1000.0
            self._unit = int(round(per_ms * fmt.ptime_ms))
        self.packets = 0

    def _ts(self) -> int:
        return (self._ts0 + self._samples * self.fmt.clock // self.fmt.sample_rate) & 0xFFFFFFFF

    def _emit(self, payload: bytes, marker: bool = False) -> bytes:
        pkt = build_rtp(self.pt, self.seq, self._ts(), self.ssrc, payload, marker)
        self.seq = (self.seq + 1) & 0xFFFF
        self.packets += 1
        return pkt

    def pack(self, chunks: list[bytes]) -> list[bytes]:
        out: list[bytes] = []
        if self.fmt.spec.frame_based:
            for frame in chunks:  # one Layer II frame; fragmented when it does not fit
                pieces = [frame[i:i + MTU_PAYLOAD - 4] for i in range(0, len(frame), MTU_PAYLOAD - 4)] or [b""]
                offset = 0
                for k, piece in enumerate(pieces):
                    out.append(self._emit(struct.pack("!HH", 0, offset) + piece, marker=k == len(pieces) - 1))
                    offset += len(piece)
                self._samples += MP2_SAMPLES_PER_FRAME
            return out
        for c in chunks:
            self._buf += c
        while len(self._buf) >= self._unit:
            piece = bytes(self._buf[:self._unit])
            del self._buf[:self._unit]
            out.append(self._emit(piece))
            self._samples += self.fmt.sample_rate * self.fmt.ptime_ms // 1000
        return out


def depacketize(fmt: AudioFormat, packet: RtpPacket) -> bytes:
    """The codec data of one packet (for Layer II without the payload header, fragments are simply appended
    to each other - the decoder's frame parser puts them back together)."""
    if fmt.spec.frame_based:
        return packet.payload[4:] if len(packet.payload) > 4 else b""
    return packet.payload


class SequenceFilter:
    """Drops duplicated and late packets and counts the lost ones (per SSRC, like the gateway's WebRTC side)."""

    def __init__(self) -> None:
        self.ssrc: int | None = None
        self.last: int | None = None
        self.lost = 0
        self.received = 0
        self.late = 0

    def accept(self, packet: RtpPacket) -> bool:
        if self.ssrc != packet.ssrc:  # a new stream (the sender restarted): begin again
            self.ssrc, self.last = packet.ssrc, None
        if self.last is not None:
            gap = (packet.seq - self.last) & 0xFFFF
            if gap == 0 or gap >= 0x8000:
                self.late += 1
                return False
            self.lost += gap - 1
        self.last = packet.seq
        self.received += 1
        return True
