"""The SIP bridge with its status page, without an intercom server: the lines are tone generators / recorders, the SIP side
is real (registered at the Kamailio test servers of dev/sipserver). To look at the page and press the buttons:

    docker compose up -d --build            (in dev/sipserver, plus  --profile second  for the second server)
    docker run -d --name sip-ui-demo --network sipserver_default -p 127.0.0.1:8088:8088 -v <repo>:/app:ro -w /app \\
        -e PYTHONPATH=/app sip-py python dev/sip_ui_demo.py
    then open http://127.0.0.1:8088   (user admin, password demo)

Calls to try: from the page, number 1234 on the account "intern" reaches whoever is registered at the first server as
1234; baresip clients (alice, bob, bob2 of the compose test) can call "bridge" / 9001."""
from __future__ import annotations

import asyncio
import socket
import sys
import time

import numpy as np

from gateway.audio import Level, MixedPlayout
from intercom_sip import calls as calls_mod
from intercom_sip.calls import SipService
from intercom_sip.config import parse_sip_config
from intercom_sip.web import WebServer


class FakeAudio:
    def __init__(self):
        self.out_buf = MixedPlayout(target_ms=60)
        self.out_level = Level()
        self.track = self

    def push(self, x):
        pass


async def feed(binding, freq, level):
    n, t0 = 0, time.monotonic()
    while True:
        t = (np.arange(480) + n * 480) / 48000
        binding.audio.out_buf.write((level * np.sin(2 * np.pi * freq * t)).astype(np.float32))
        n += 1
        await asyncio.sleep(max(0, t0 + n * 0.01 - time.monotonic()))


async def main() -> None:
    async def nothing(self):
        return

    calls_mod.CallRunner._start_lines = nothing
    calls_mod.CallRunner._stop_lines = nothing
    ip = socket.gethostbyname(socket.gethostname())
    cfg = parse_sip_config({
        "server": {"url": "http://127.0.0.1:1"},
        "sip": {"sip_port": 5080, "local_ip": ip, "rtp_port_min": 41000, "rtp_port_max": 41040},
        "web": {"listen": "0.0.0.0:8088", "user": "admin", "password": "demo"},
        "account": [
            {"name": "intern", "registrar": "sip:kamailio:5060", "user": "bridge", "password": "bridge-pass",
             "domain": "sip.local", "expires_s": 60},
            {"name": "extern", "registrar": "sip:kamailio2:5060", "user": "9001", "password": "pw9001",
             "domain": "sip.other", "expires_s": 60},
        ],
        "line": [{"name": n, "production_id": "1", "line_id": "1", "username": n, "password": "x"}
                 for n in ("Regie-SAW", "Kommentar", "N-1", "Regie-Mainz", "Technik-Mainz")],
        "call": [
            {"name": "SAW Mainz", "account": "intern", "mode": "both", "target": "1234", "codec": "mp2",
             "bitrate_kbps": 192, "channel_mode": "dual-mono", "rtp_latch": True,
             "left": {"line": "Regie-SAW"}, "right": {"send": "Kommentar", "receive": "N-1"}},
            {"name": "ZDF Mainz", "account": "extern", "mode": "both", "codec": "g722", "rtp_latch": True,
             "left": {"line": "Regie-Mainz"}},
            {"name": "Studio Hamburg (nur Annahme)", "mode": "answer", "accept_from": ["127.0.0.1"], "codec": "mp2",
             "bitrate_kbps": 128, "channel_mode": "stereo", "left": {"line": "Technik-Mainz"}},
        ],
    })
    svc = SipService(cfg, None)
    await svc.start()
    for r in svc.runners:
        for b in r.bindings.values():
            b.audio = FakeAudio()
    web = WebServer(svc, cfg.web)
    await web.start()
    # the "intercom" side: the lines carry some sound, so the levels move
    tones = {"Regie-SAW": (440, 0.2), "Kommentar": (660, 0.1), "Regie-Mainz": (550, 0.15)}
    feeders = [asyncio.ensure_future(feed(r.bindings[n], *tones[n])) for r in svc.runners for n in r.bindings if n in tones]
    try:
        await svc.run()
    finally:
        for f in feeders:
            f.cancel()
        await web.close()
        await svc.close()


sys.exit(asyncio.run(main()))
