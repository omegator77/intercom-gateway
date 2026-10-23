"""Session descriptions (RFC 4566) for one audio stream: what we offer, and what the other side offered or answered."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .codecs import AudioFormat, BY_RTP_NAME, CODECS, CodecDef

DYNAMIC_PT = 96
# payload types fixed by RFC 3551 that we know, besides the ones in CODECS: (name, clock, channels)
STATIC_EXTRA = {10: ("L16", 44100, 2), 11: ("L16", 44100, 1)}


class SdpError(Exception):
    pass


@dataclass
class RemoteCodec:
    pt: int
    name: str  # encoding name as sent, upper-cased
    clock: int
    channels: int
    fmtp: str = ""

    @property
    def spec(self) -> CodecDef | None:
        return BY_RTP_NAME.get(self.name)


@dataclass
class RemoteSdp:
    address: str  # where to send the RTP
    port: int
    codecs: list[RemoteCodec] = field(default_factory=list)  # in the order of the m-line = the other side's preference
    ptime_ms: int | None = None
    direction: str = "sendrecv"

    @property
    def on_hold(self) -> bool:
        return self.port == 0 or self.direction in ("inactive", "recvonly") or self.address == "0.0.0.0"


def payload_type(fmt: AudioFormat) -> int:
    return fmt.spec.static_pt if fmt.spec.static_pt is not None else DYNAMIC_PT


def rtpmap(fmt: AudioFormat) -> str:
    spec = fmt.spec
    line = f"{spec.rtp_name}/{fmt.clock}"
    if fmt.codec == "l16":  # RFC 3551 names MPA without a channel count: mono/stereo/dual is in the stream itself
        line += f"/{fmt.channels}"
    return line


def build_sdp(ip: str, port: int, fmt: AudioFormat, session_id: int | None = None, fmtp: str = "",
              pt: int | None = None) -> str:
    """An offer or an answer naming exactly one codec (`pt`: the payload type number to use instead of the default)."""
    pt = payload_type(fmt) if pt is None else pt
    sid = session_id or int(time.time())
    lines = [
        "v=0",
        f"o=intercom-sip {sid} {sid} IN IP4 {ip}",
        "s=Open Intercom",
        f"c=IN IP4 {ip}",
        "t=0 0",
        f"m=audio {port} RTP/AVP {pt}",
        f"a=rtpmap:{pt} {rtpmap(fmt)}",
    ]
    if fmtp:
        lines.append(f"a=fmtp:{pt} {fmtp}")
    if not fmt.spec.frame_based:  # a Layer II packet is one frame (24 ms at 48 kHz); a packet time would only confuse
        lines.append(f"a=ptime:{fmt.ptime_ms}")
    lines.append("a=sendrecv")
    return "\r\n".join(lines) + "\r\n"


def describe_sdp(text: str) -> str:
    """The lines of a session description that matter for the audio, on one line (for the log: the first thing to
    look at when a codec of another make does not interoperate)."""
    keep = ("c=", "m=", "a=rtpmap", "a=fmtp", "a=ptime", "a=maxptime", "a=sendrecv", "a=sendonly", "a=recvonly",
            "a=inactive", "a=rtcp")
    return " | ".join(l.strip() for l in text.replace("\r\n", "\n").split("\n") if l.startswith(keep))


def parse_sdp(text: str) -> RemoteSdp:
    """The first active audio stream of a session description."""
    session_addr: str | None = None
    media: dict | None = None
    medias: list[dict] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if len(line) < 2 or line[1] != "=":
            continue
        kind, value = line[0], line[2:]
        if kind == "m":
            parts = value.split()
            media = {"kind": parts[0], "port": int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0,
                     "proto": parts[2] if len(parts) > 2 else "", "pts": [int(p) for p in parts[3:] if p.isdigit()],
                     "addr": None, "rtpmap": {}, "fmtp": {}, "ptime": None, "dir": None}
            medias.append(media)
        elif kind == "c":
            fields = value.split()
            addr = fields[2].split("/")[0] if len(fields) >= 3 else None
            if media is None:
                session_addr = addr
            else:
                media["addr"] = addr
        elif kind == "a":
            name, _, arg = value.partition(":")
            target = media
            if target is None:
                continue
            if name == "rtpmap":
                pt, _, enc = arg.partition(" ")
                if pt.isdigit():
                    target["rtpmap"][int(pt)] = enc.strip()
            elif name == "fmtp":
                pt, _, params = arg.partition(" ")
                if pt.isdigit():
                    target["fmtp"][int(pt)] = params.strip()
            elif name == "ptime" and arg.strip().isdigit():
                target["ptime"] = int(arg.strip())
            elif name in ("sendrecv", "sendonly", "recvonly", "inactive"):
                target["dir"] = name
    audio = next((m for m in medias if m["kind"] == "audio" and m["port"] != 0 and m["proto"].startswith("RTP/AVP")),
                 None)
    if audio is None:
        raise SdpError("no usable audio stream in the session description")
    address = audio["addr"] or session_addr
    if not address:
        raise SdpError("the session description names no address for the audio")
    codecs: list[RemoteCodec] = []
    for pt in audio["pts"]:
        enc = audio["rtpmap"].get(pt)
        if enc:
            parts = enc.split("/")
            name = parts[0].upper()
            clock = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 8000
            channels = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        else:
            known = next((c for c in CODECS.values() if c.static_pt == pt), None)
            if known is not None:
                name, clock, channels = known.rtp_name.upper(), known.clock, 1
            elif pt in STATIC_EXTRA:
                name, clock, channels = STATIC_EXTRA[pt]
            else:
                continue
        codecs.append(RemoteCodec(pt, name, clock, channels, audio["fmtp"].get(pt, "")))
    return RemoteSdp(address, audio["port"], codecs, audio["ptime"], audio["dir"] or "sendrecv")


def choose_codec(remote: RemoteSdp, wanted: AudioFormat | None = None) -> tuple[RemoteCodec, AudioFormat]:
    """The codec for a call that somebody else offered: the first one on their list that we can do (they are on
    "auto" and follow their own encoder). If `wanted` is given, the entry matching it is taken instead."""
    candidates: list[tuple[RemoteCodec, AudioFormat]] = []
    for rc in remote.codecs:
        spec = rc.spec
        if spec is None:
            continue
        if spec.key == "mp2":
            # the stream says nothing about its rate and mode: we take ours and decode what actually arrives
            base = wanted if wanted is not None and wanted.codec == "mp2" else AudioFormat(
                "mp2", 48000, 2, 192, "stereo")
            candidates.append((rc, AudioFormat("mp2", base.sample_rate, base.channels, base.bitrate_kbps, base.mode,
                                               remote.ptime_ms or 20)))
        elif spec.key == "l16":
            if rc.clock in spec.sample_rates and rc.channels in spec.channels:
                candidates.append((rc, AudioFormat("l16", rc.clock, rc.channels, 0, "stereo" if rc.channels == 2
                                                   else "mono", remote.ptime_ms or 20)))
        else:
            candidates.append((rc, AudioFormat(spec.key, spec.sample_rates[0], 1, 0, "mono", remote.ptime_ms or 20)))
    if not candidates:
        raise SdpError("none of the offered codecs is supported (supported: "
                       + ", ".join(sorted(c.rtp_name for c in CODECS.values())) + ")")
    if wanted is not None:
        for rc, fmt in candidates:
            if fmt.codec == wanted.codec:
                return rc, fmt
    return candidates[0]
