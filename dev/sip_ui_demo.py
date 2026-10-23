"""The SIP bridge with its status and settings page, without audio hardware or a WebRTC intercom: the SIP side is real
(registered at the Kamailio test servers of dev/sipserver), the lines are tone generators, and "load lines" in the line form
asks a real intercom manager (the local test stack, `python dev/stack.py up && python dev/stack.py setup`).

    docker run -d --name sip-ui-demo --network sipserver_default -p 127.0.0.1:8088:8088 \\
        -v <repo>:/app:ro -v <repo>/dev/.state/sipdemo:/data -w /app -e PYTHONPATH=/app \\
        -e MANAGER_URL=http://host.docker.internal:8001 sip-py python dev/sip_ui_demo.py
    then open http://127.0.0.1:8088   (user admin, password demo)

Settings made on the page are kept in /data/sip-data.json; delete that file for a fresh start."""
from __future__ import annotations

import asyncio
import os
import socket
import sys
import time
import zlib
from pathlib import Path

import numpy as np

from gateway.audio import Level, MixedPlayout
from intercom_sip import calls as calls_mod
from intercom_sip.admin import ConfigManager
from intercom_sip.calls import SipService
from intercom_sip.config import merge_raw, parse_sip_config
from intercom_sip.store import Store
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


async def fake_start_lines(self):
    """Instead of joining real intercom lines: every line carries a tone of its own."""
    for name, b in self.bindings.items():
        if b.audio is None:
            b.audio = FakeAudio()
            freq = 300 + zlib.crc32(name.encode()) % 700
            self._demo_feeders = getattr(self, "_demo_feeders", []) + [asyncio.ensure_future(feed(b, freq, 0.15))]


async def fake_stop_lines(self):
    for f in getattr(self, "_demo_feeders", []):
        f.cancel()
    self._demo_feeders = []
    for b in self.bindings.values():
        b.audio = None


async def main() -> None:
    calls_mod.CallRunner._start_lines = fake_start_lines
    calls_mod.CallRunner._stop_lines = fake_stop_lines
    ip = socket.gethostbyname(socket.gethostname())
    toml = {
        "server": {"url": os.environ.get("MANAGER_URL", "http://host.docker.internal:8001")},
        "sip": {"sip_port": 5080, "local_ip": ip, "rtp_port_min": 41000, "rtp_port_max": 41040},
        "web": {"listen": "0.0.0.0:8088", "user": "admin", "password": "demo"},
    }
    store = Store(Path(os.environ.get("STORE", "/data/sip-data.json")))
    store.load()
    cfg = parse_sip_config(merge_raw(toml, store.raw()), lookup_password=lambda *_: None, require_calls=False)
    svc = SipService(cfg, None)
    await svc.start()
    web = WebServer(svc, cfg.web, ConfigManager(svc, toml, store, lookup_password=lambda *_: None))
    await web.start()
    try:
        await svc.run()
    finally:
        await web.close()
        await svc.close()


sys.exit(asyncio.run(main()))
