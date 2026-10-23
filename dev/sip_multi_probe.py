"""Two SIP accounts at two different servers at once - the way a hardware codec is registered at several
registrars - with one call profile per account, and no intercom server needed (the lines are tone generators).

    python dev/sip_multi_probe.py --mode answer     # wait until both accounts have been called, report who got which call
    python dev/sip_multi_probe.py --mode dial       # call through both accounts

Meant to run inside the compose network of dev/sipserver (docker compose --profile second up -d), with baresip clients
registered at both servers (see the README of the SIP service). Exit code 0 = both calls came up with the right peer."""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

import numpy as np

from gateway.audio import Level, MixedPlayout, tone_amplitude_near
from intercom_sip import calls as calls_mod
from intercom_sip.calls import CallState, SipService
from intercom_sip.config import parse_sip_config


class FakeAudio:
    def __init__(self):
        self.out_buf = MixedPlayout(target_ms=60)
        self.out_level = Level()
        self.pushed: list[np.ndarray] = []
        self.track = self

    def push(self, x):
        self.pushed.append(np.array(x, copy=True))


async def feed(binding, freq):
    n, t0 = 0, time.monotonic()
    while True:
        t = (np.arange(480) + n * 480) / 48000
        binding.audio.out_buf.write((0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32))
        n += 1
        await asyncio.sleep(max(0, t0 + n * 0.01 - time.monotonic()))


async def main(a) -> int:
    async def nothing(self):
        return

    calls_mod.CallRunner._start_lines = nothing
    calls_mod.CallRunner._stop_lines = nothing
    accounts = [
        {"name": "intern", "registrar": "sip:kamailio:5060", "user": "bridge", "password": "bridge-pass",
         "domain": "sip.local", "expires_s": 60},
        {"name": "extern", "registrar": "sip:kamailio2:5060", "user": "9001", "password": "pw9001",
         "domain": "sip.other", "expires_s": 60},
    ]
    base = {"codec": "g722", "rtp_latch": True, "redial": False, "mode": a.mode}
    calls = [{"name": "in-intern" if a.mode == "answer" else "to-bob", "account": "intern", "left": {"line": "A"}, **base},
             {"name": "in-extern" if a.mode == "answer" else "to-bob2", "account": "extern", "left": {"line": "B"}, **base}]
    if a.mode == "dial":
        calls[0]["target"], calls[1]["target"] = "bob", "bob2"
    cfg = parse_sip_config({
        "server": {"url": "http://127.0.0.1:1"},
        "sip": {"sip_port": 5080, "local_ip": a.local_ip, "rtp_port_min": 41000, "rtp_port_max": 41040},
        "account": accounts,
        "line": [{"name": n, "production_id": "1", "line_id": "1", "username": n, "password": "x"} for n in ("A", "B")],
        "call": calls})
    svc = SipService(cfg, None)
    await svc.start()
    for r in svc.runners:
        for b in r.bindings.values():
            b.audio = FakeAudio()
    run = asyncio.ensure_future(svc.run())
    ok = False
    try:
        end = time.monotonic() + a.wait
        while time.monotonic() < end and not all(r.state == CallState.CONNECTED for r in svc.runners):
            await asyncio.sleep(0.2)
        print("registrations:", [(x["account"], x["address"], x["state"]) for x in svc.status()["registrations"]])
        peers = {}
        for r, freq in zip(svc.runners, (1000, 2000)):
            peers[r.cfg.name] = r.dialog.remote_uri if r.dialog else None
            print(f"call {r.cfg.name!r}: {r.state.value}, peer {peers[r.cfg.name]}, account {r.cfg.account}")
        if all(r.state == CallState.CONNECTED for r in svc.runners):
            feeders = [asyncio.ensure_future(feed(r.bindings[r.cfg.channels[0].send], f))
                       for r, f in zip(svc.runners, (1000, 2000))]
            await asyncio.sleep(a.seconds)
            for f in feeders:
                f.cancel()
            for r in svc.runners:
                st = r.media.stats()
                print(f"  {r.cfg.name}: tx {st['tx_packets']} rx {st['rx_packets']} lost {st['rx_lost']}")
            want = ("alice", "bob2") if a.mode == "answer" else ("bob", "bob2")
            ok = all(w in (peers[r.cfg.name] or "") for r, w in zip(svc.runners, want)) and all(
                r.media.stats()["rx_packets"] > 50 for r in svc.runners)
    finally:
        svc.stop()
        await asyncio.wait_for(run, 30)
        await svc.close()
    print("result:", "OK" if ok else "FAILED")
    return 0 if ok else 1


ap = argparse.ArgumentParser()
ap.add_argument("--mode", choices=["answer", "dial"], required=True)
ap.add_argument("--local-ip", required=True)
ap.add_argument("--wait", type=float, default=40.0)
ap.add_argument("--seconds", type=float, default=5.0)
sys.exit(asyncio.run(main(ap.parse_args())))
