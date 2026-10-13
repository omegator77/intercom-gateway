"""Debug helper: one peer, dump SDP summaries, transceiver directions and stats."""
import asyncio
import json
import logging
import sys

import httpx

from gwclient import Peer, ToneTrack, login

BASE = "http://127.0.0.1:8001"
logging.getLogger("aiortc").setLevel(logging.WARNING)
logging.getLogger("aioice").setLevel(logging.WARNING)


def interesting(sdp: str):
    keep = ("m=", "a=sendrecv", "a=sendonly", "a=recvonly", "a=inactive", "a=mid", "a=setup", "a=rtpmap",
            "a=fmtp", "a=extmap", "a=sctp", "a=ice-lite", "a=ice-options", "a=rtcp-mux", "a=group", "a=ssrc-group")
    out = []
    ssrcs = set()
    for line in sdp.splitlines():
        if line.startswith("a=ssrc:"):
            ssrcs.add(line.split()[0])
        if line.startswith(keep) and "fingerprint" not in line:
            out.append(line)
        if line.startswith("a=candidate"):
            out.append(line.split("generation")[0])
    out.append(f"(distinct a=ssrc ids: {len(ssrcs)})")
    return "\n    ".join(out)


async def main(state_file, relay):
    st = json.load(open(state_file))
    async with httpx.AsyncClient(base_url=BASE, timeout=20) as anon:
        u = st["users"]["alice"]
        tok = await login(anon, u["username"], u["password"])
    p = Peer("alice", BASE, tok, ToneTrack())
    p.track.freq = 440.0
    got_track = []
    try:
        await p.connect(st["productionId"], st["lineId"], "Alice", relay)
        got_track.append(len(p.pc.getReceivers()))
        await asyncio.sleep(4)
        print("OFFER:\n    " + interesting(p.offer_sdp))
        print("ANSWER:\n    " + interesting(p.answer_sdp))
        for t in p.pc.getTransceivers():
            print("transceiver:", t.kind, "mid", t.mid, "direction", t.direction, "current", t.currentDirection)
        print("states: conn", p.pc.connectionState, "ice", p.pc.iceConnectionState, "gather", p.pc.iceGatheringState,
              "sctp", (p.pc.sctp.state if p.pc.sctp else None), "sctp-transport", (p.pc.sctp.transport.state if p.pc.sctp else None), "assoc", (p.pc.sctp._association_state if p.pc.sctp else None))
        print("dc open:", p.dc_open, "| remote frames received:", len(p.sink.frames))
        stats = await p.pc.getStats()
        for k, v in stats.items():
            d = {a: getattr(v, a) for a in ("type", "packetsSent", "packetsReceived", "bytesSent", "bytesReceived", "packetsLost", "jitter") if hasattr(v, a)}
            print("stat", k, d)
    finally:
        await p.close()


asyncio.run(asyncio.wait_for(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "mixed"), 60))
