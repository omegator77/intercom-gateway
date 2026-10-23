"""Dials a SIP target with the SIP service and prints what happened - no intercom server needed (the lines are
replaced by tone generators and recorders). Meant for testing against other manufacturers' codecs / softphones, e.g.
inside a Docker network next to baresip:

    python dev/sip_dial_probe.py sip:test@bs3:5070 --codec mp2 --seconds 10 --local-ip 172.30.0.5

Left channel sends 1 kHz, right 3 kHz; what comes back is analysed per channel. Exit code 0 = call came up and
audio flowed in both directions."""
from __future__ import annotations

import argparse
import asyncio
import json
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
    spec = {"mp2": dict(codec="mp2", bitrate_kbps=192, channel_mode="dual-mono" if a.dual else "stereo"),
            "g722": dict(codec="g722"), "pcmu": dict(codec="pcmu"), "pcma": dict(codec="pcma"),
            "l16": dict(codec="l16", channel_mode="stereo")}[a.codec]
    chans = {"left": {"line": "A"}} if spec["codec"] in ("g722", "pcmu", "pcma") else {"left": {"line": "A"}, "right": {"line": "B"}}
    cfg = parse_sip_config({
        "server": {"url": "http://127.0.0.1:1"},
        "sip": {"sip_port": a.sip_port, "local_ip": a.local_ip, "rtp_port_min": 41000, "rtp_port_max": 41040},
        "line": [{"name": n, "production_id": "1", "line_id": "1", "username": n, "password": "x"} for n in ("A", "B")],
        "call": [{"name": "probe", "mode": "dial", "target": a.target, "redial": False, "rtp_latch": True,
                  **spec, **chans}]})
    svc = SipService(cfg, None)
    await svc.start()
    r = svc.runners[0]
    for b in r.bindings.values():
        b.audio = FakeAudio()
    run = asyncio.ensure_future(svc.run())
    ok = False
    try:
        end = time.monotonic() + 15
        while time.monotonic() < end and r.state != CallState.CONNECTED and not run.done():
            await asyncio.sleep(0.1)
        if r.state == CallState.CONNECTED:
            feeders = [asyncio.ensure_future(feed(r.bindings["A"], 1000)), asyncio.ensure_future(feed(r.bindings["B"], 3000))]
            await asyncio.sleep(a.seconds)
            for f in feeders:
                f.cancel()
            st = r.media.stats()
            print("media:", json.dumps(st))
            for name in ("A", "B"):
                chunks = r.bindings[name].audio.pushed
                x = np.concatenate(chunks)[-48000:] if chunks else np.zeros(0, np.float32)
                print(f"heard on {name}: 1 kHz {tone_amplitude_near(x, 1000):.3f}  3 kHz {tone_amplitude_near(x, 3000):.3f}")
            ok = st["rx_packets"] > 50 and st["tx_packets"] > 50
        else:
            print("the call did not come up:", r.state.value, r.last_error)
    finally:
        svc.stop()
        await asyncio.wait_for(run, 20)
        await svc.close()
    print("result:", "OK" if ok else "FAILED")
    return 0 if ok else 1


ap = argparse.ArgumentParser()
ap.add_argument("target")
ap.add_argument("--codec", default="mp2", choices=["mp2", "g722", "pcmu", "pcma", "l16"])
ap.add_argument("--dual", action="store_true", help="Layer II: flag the stream as dual channel instead of stereo")
ap.add_argument("--seconds", type=float, default=8.0)
ap.add_argument("--local-ip", default=None)
ap.add_argument("--sip-port", type=int, default=5080)
sys.exit(asyncio.run(main(ap.parse_args())))
