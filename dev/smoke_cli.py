"""Smoke test for the real CLI entry point: `gateway run` against the local stack with synthetic audio,
stopped with a genuine SIGINT after a few seconds. Checks that it connects, reports status, shuts down
cleanly (exit code 0) and leaves no session behind on the server.

    python dev/smoke_cli.py
"""
from __future__ import annotations

import re
import signal
import sys
import threading
import time
from pathlib import Path

import httpx

from gateway.__main__ import main

STATE = Path(__file__).parent / ".state"
LOG = STATE / "gateway.log"
RUN_FOR_S = 9.0

if LOG.exists():
    LOG.unlink()
threading.Timer(RUN_FOR_S, lambda: signal.raise_signal(signal.SIGINT)).start()
t0 = time.monotonic()
code = main(["run", "-c", str(STATE / "gateway.local.toml"), "--log-file", str(LOG), "--status-every", "3", "--state-dir", str(STATE)])
took = time.monotonic() - t0

text = LOG.read_text(encoding="utf-8")
sessions = re.findall(r"connected \(session ([0-9a-f-]{36})", text)
ok = {
    "exit code 0": code == 0,
    "both lines connected": len(sessions) == 2,
    "status lines were logged": "in " in text and "dBFS" in text,
    "stopped within 5 s of SIGINT": took < RUN_FOR_S + 5,
    "logged 'stopped'": text.rstrip().endswith("stopped"),
}
with httpx.Client(base_url="http://127.0.0.1:8001") as http:
    gone = [http.get(f"/api/v1/heartbeat/{s}").status_code == 410 for s in sessions]
ok["sessions removed from the server on exit"] = bool(gone) and all(gone)
for name, passed in ok.items():
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
print(f"ran {took:.1f} s")
sys.exit(0 if all(ok.values()) else 1)
