"""Spike S3: three synthetic participants (alice 440 Hz, carol 880 Hz, bob listening)
join one line on the LOCAL stack. Checks handshake, heartbeat, data channel,
mix-minus-self and onset latency. `--bob-relay ssrc-rewrite` repeats it with the
browser's relay mode on the listener, to show what a single-jitter-buffer client
does with several simultaneous ssrcs."""
import argparse
import asyncio
import json
import logging
import time

import httpx

from gwclient import API, Peer, ToneTrack, login, tone_amp

BASE = "http://127.0.0.1:8001"
logging.getLogger("aiortc").setLevel(logging.WARNING)
logging.getLogger("aioice").setLevel(logging.WARNING)


async def main(state_file: str, bob_relay: str | None):
    assert BASE.startswith("http://127.0.0.1"), "spike must only talk to localhost"
    st = json.load(open(state_file))
    pid, lid = st["productionId"], st["lineId"]

    peers: dict[str, Peer] = {}
    async with httpx.AsyncClient(base_url=BASE, timeout=20) as anon:
        for who in ("alice", "bob", "carol"):
            u = st["users"][who]
            tok = await login(anon, u["username"], u["password"])
            peers[who] = Peer(who, BASE, tok, ToneTrack())

    try:
        relay = {"alice": "mixed", "carol": "mixed", "bob": bob_relay or "mixed"}
        await asyncio.gather(*[
            peers[w].connect(pid, lid, w.title(), relay[w]) for w in peers
        ])
        print("connected; timings (ms):", {w: {k: round(v) for k, v in p.timings.items()} for w, p in peers.items()})

        hb = {w: await p.heartbeat() for w, p in peers.items()}
        print("heartbeat status:", hb)

        t_begin = time.monotonic()
        await asyncio.sleep(2.0)                      # baseline silence
        peers["alice"].track.freq = 440.0             # alice starts talking
        t_alice = time.monotonic()
        await asyncio.sleep(4.0)
        peers["carol"].track.freq = 880.0             # carol joins in
        t_carol = time.monotonic()
        await asyncio.sleep(5.0)
        peers["alice"].track.freq = None
        peers["carol"].track.freq = None
        t_end = time.monotonic()
        await asyncio.sleep(1.5)

        def amps(who, t0, t1):
            x = peers[who].sink.window(t0, t1)
            return round(tone_amp(x, 440)), round(tone_amp(x, 880)), len(x)

        w_single = (t_alice + 1.5, t_carol - 0.3)     # only alice talking
        w_both = (t_carol + 1.5, t_end - 0.3)         # alice + carol talking
        print("\nexpected: tone amplitude ~8000 where present, ~0 where absent")
        print(f"{'listener':8} | only alice talks: 440Hz 880Hz | both talk: 440Hz 880Hz | samples")
        for who in ("bob", "alice", "carol"):
            a1 = amps(who, *w_single)
            a2 = amps(who, *w_both)
            print(f"{who:8} |                  {a1[0]:5} {a1[1]:5} |             {a2[0]:5} {a2[1]:5} | {a2[2]}")

        # onset latency: first decoded frame at bob after alice started whose RMS is clearly above silence
        import numpy as np
        bob = peers["bob"].sink
        onset = next((t for t, a in bob.frames if t >= t_alice and len(a) and float(np.sqrt(np.mean(a.astype(float) ** 2))) > 300), None)
        t_sent = peers["alice"].track.first_tone_t
        if onset and t_sent:
            print(f"\nonset latency alice->bob (frame creation to decoded frame): {round((onset - t_sent) * 1000)} ms")
        else:
            print("\nonset latency: could not detect")
        # loudness: what SMB's mix does to a 8000-amplitude sine
        print("level: sent amplitude 8000 -> received", round(tone_amp(bob.window(*w_single), 440)),
              f"({20*np.log10(max(tone_amp(bob.window(*w_single), 440),1)/8000):.1f} dB)")
        print("data channel open:", {w: p.dc_open for w, p in peers.items()})
        for w, p in peers.items():
            st_ = await p.pc.getStats()
            row = {}
            for v in st_.values():
                if v.type in ("inbound-rtp", "outbound-rtp"):
                    row[v.type] = getattr(v, "packetsReceived", None) if v.type == "inbound-rtp" else getattr(v, "packetsSent", None)
            print(f"rtp packets at {w}:", row, "| decoded frames:", len(p.sink.frames),
                  "| remote track seen:", p._sink_task is not None)
        for w, p in peers.items():
            if p.dc_messages:
                print(f"data channel msgs at {w} (first 3):", p.dc_messages[:3])
    finally:
        await asyncio.gather(*[p.close() for p in peers.values()], return_exceptions=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("state")
    ap.add_argument("--bob-relay", default=None, choices=[None, "mixed", "ssrc-rewrite"])
    a = ap.parse_args()
    asyncio.run(asyncio.wait_for(main(a.state, a.bob_relay), 90))
