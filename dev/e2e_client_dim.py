"""End-to-end check of dimming the programme line in the client, against the local test stack.

Needs a stack where the line "Technik" is the programme line (the setup in the e2e docstring of stack.py creates
none, so before running mark it and restart the manager):

    docker exec gw-dev-mongo mongosh --quiet gateway-dev --eval \
        'db.productions.updateOne({_id:1,"lines.id":"2"},{$set:{"lines.$.programOutputLine":true}})'
    python dev/stack.py manager main

Two clients on Regie (a normal line) and Technik (the programme line). A latches its microphone tone into the
programme line (so B hears it there), and then also into Regie: B hears somebody speaking to it, and B's
programme line has to drop by 14 dB after the reaction time, and come back when A stops. Then the same for B
speaking itself.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from intercom_client.config import parse_client_config
from intercom_client.engine import ClientEngine
from intercom_client.hub import SyntheticHub

STATE = json.loads((Path(__file__).parent / ".state" / "state.json").read_text())
BASE = STATE["base"]
assert BASE.startswith("http://127.0.0.1"), "must only talk to the local stack"
results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {detail}")


def make(who: str, tone: float) -> ClientEngine:
    u = STATE["users"][who]
    cfg = parse_client_config({
        "server": {"url": BASE, "username": u["username"]},
        "audio": {"backend": "synthetic"},
        "dim": {"delay_ms": 500},
        "line": [{"name": "Regie", "production_id": STATE["productionId"], "line_id": STATE["lines"]["Regie"]},
                 {"name": "Programm", "production_id": STATE["productionId"], "line_id": STATE["lines"]["Technik"]}],
    }, env={"INTERCOM_CLIENT_PASSWORD": u["password"]})
    return ClientEngine(cfg, enable_hotkeys=False, hub_factory=lambda a: SyntheticHub(a, tone_hz=tone))


def wait(cond, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


def prog(eng: ClientEngine) -> dict:
    return next(l for l in eng.snapshot()["lines"] if l["name"] == "Programm")


def settled(eng: ClientEngine, seconds: float = 1.2) -> float:
    time.sleep(seconds)
    return prog(eng)["out_dbfs"]


def main() -> int:
    a, b = make("human_regie", 440.0), make("human_technik", 660.0)
    a.start()
    b.start()
    try:
        ok = wait(lambda: all(l["state"] == "connected" for e in (a, b) for l in e.snapshot()["lines"]))
        check("both clients are connected on both lines", ok)
        if not ok:
            return 1
        flagged = wait(lambda: b.controls["Programm"].program and not b.controls["Regie"].program, 20)
        check("the client learns from the server which line is the programme line", flagged,
              str([(l["name"], l["program"]) for l in b.snapshot()["lines"]]))
        if not flagged:
            return 1

        a.toggle_latch("Programm")
        wait(lambda: prog(b)["out_dbfs"] > -40, 10)
        quiet = settled(b, 2.0)
        check("B hears the programme line", quiet > -40, f"{quiet:.1f} dB")
        check("and it is not dimmed", not b.snapshot()["dimmed"])

        a.toggle_latch("Regie")  # somebody is now speaking to B on Regie
        dimmed = wait(lambda: b.snapshot()["dimmed"], 6)
        low = settled(b, 1.0)
        check("speech on Regie dims B's programme line", dimmed, f"dimmed={b.snapshot()['dimmed']}")
        check("by about 14 dB", 12.0 < quiet - low < 16.0, f"{quiet:.1f} -> {low:.1f} dB ({quiet - low:.1f})")
        check("the card data says Programm + gedimmt", prog(b)["program"] and prog(b)["dimmed"])

        a.toggle_latch("Regie")
        released = wait(lambda: not b.snapshot()["dimmed"], 6)
        back = settled(b, 1.0)
        check("when the speech ends the dim is released", released and abs(back - quiet) < 2.0,
              f"{low:.1f} -> {back:.1f} dB")

        b.ptt("Regie", True)  # B speaks itself
        own = wait(lambda: b.snapshot()["dimmed"], 6)
        check("B speaking into Regie dims B's own programme line too", own)
        check("and A's, because A is being spoken to on Regie", wait(lambda: a.snapshot()["dimmed"], 6))
        b.ptt("Regie", False)
        check("both come back when B stops", wait(lambda: not b.snapshot()["dimmed"] and not a.snapshot()["dimmed"], 6))
    finally:
        a.stop()
        b.stop()
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


sys.exit(main())
