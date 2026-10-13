"""Spike S4-lite: does a Python PortAudio callback stay on time while aiortc encodes/decodes
several peers in the same process? Uses a SILENT output stream on the notebook speakers
(never a DVS device) and the local spike stack. Reports callback interval statistics."""
import asyncio
import json
import logging
import sys
import time

import httpx
import numpy as np
import sounddevice as sd

from gwclient import Peer, ToneTrack, login

BASE = "http://127.0.0.1:8001"
logging.getLogger("aiortc").setLevel(logging.WARNING)
logging.getLogger("aioice").setLevel(logging.WARNING)
DURATION = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0


async def main(state_file):
    assert BASE.startswith("http://127.0.0.1")
    st = json.load(open(state_file))
    apis = {i: a["name"] for i, a in enumerate(sd.query_hostapis())}
    dev = next(i for i, d in enumerate(sd.query_devices())
               if apis[d["hostapi"]] == "Windows WASAPI" and d["max_output_channels"] and "Lautsprecher" in d["name"])

    stamps: list[float] = []
    flags = {"underflow": 0, "other": 0}

    def cb(outdata, frames, time_info, status):
        stamps.append(time.perf_counter())
        outdata.fill(0)  # digital silence
        if status:
            if status.output_underflow:
                flags["underflow"] += 1
            else:
                flags["other"] += 1

    peers = {}
    async with httpx.AsyncClient(base_url=BASE, timeout=20) as anon:
        for who in ("alice", "bob", "carol"):
            u = st["users"][who]
            peers[who] = Peer(who, BASE, await login(anon, u["username"], u["password"]), ToneTrack())
    try:
        await asyncio.gather(*[peers[w].connect(st["productionId"], st["lineId"], w.title(), "mixed") for w in peers])
        peers["alice"].track.freq = 440.0
        peers["carol"].track.freq = 880.0
        with sd.OutputStream(device=dev, samplerate=48000, channels=2, dtype="float32", blocksize=960, callback=cb):
            t0 = last_hb = time.monotonic()
            while time.monotonic() - t0 < DURATION:
                if time.monotonic() - last_hb >= 10:
                    last_hb = time.monotonic()
                    await asyncio.gather(*[p.heartbeat() for p in peers.values()])
                await asyncio.sleep(0.5)
        d = np.diff(np.array(stamps)) * 1000
        d = d[5:]  # skip start-up
        print(f"callbacks: {len(d)}  blocksize 960 (20 ms) on '{sd.query_devices(dev)['name']}'")
        print(f"interval ms: mean {d.mean():.2f}  std {d.std():.2f}  p99 {np.percentile(d, 99):.2f}  max {d.max():.2f}  min {d.min():.2f}")
        print(f"intervals > 30 ms: {(d > 30).sum()}   > 40 ms: {(d > 40).sum()}   underflows reported by PortAudio: {flags['underflow']}")
    finally:
        await asyncio.gather(*[p.close() for p in peers.values()], return_exceptions=True)


asyncio.run(asyncio.wait_for(main(sys.argv[1]), 120))
