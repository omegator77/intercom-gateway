"""End-to-end check of the intercom client against the local test stack (stack.py up && setup).

Two clients (human_regie, human_technik), both on the lines Regie and Technik, synthetic audio: each one's
"microphone" is a tone (A 440 Hz, B 660 Hz). Checks that nobody is heard until someone talks, that talking
goes only to the line talked into, hold ends it, latch keeps it, and the listener's mute and volume work.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from gateway.audio import tone_amplitude_near
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
        "line": [{"name": "Regie", "production_id": STATE["productionId"], "line_id": STATE["lines"]["Regie"]},
                 {"name": "Technik", "production_id": STATE["productionId"], "line_id": STATE["lines"]["Technik"]}],
    }, env={"INTERCOM_CLIENT_PASSWORD": u["password"]})
    return ClientEngine(cfg, enable_hotkeys=False, hub_factory=lambda a: SyntheticHub(a, tone_hz=tone))


def wait(cond, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


def heard(eng: ClientEngine, freq: float, seconds: float = 1.5) -> float:
    now = time.monotonic()
    return tone_amplitude_near(eng.hub.heard(now - seconds, now), freq)


def line(eng: ClientEngine, name: str) -> dict:
    return next(l for l in eng.snapshot()["lines"] if l["name"] == name)


def main() -> int:
    a, b = make("human_regie", 440.0), make("human_technik", 660.0)
    a.start()
    b.start()
    try:
        check("both clients start", a.running and b.running)
        ok = wait(lambda: all(line(e, n)["state"] == "connected" for e in (a, b) for n in ("Regie", "Technik")))
        check("all four line connections come up", ok, str([line(e, n)["state"] for e in (a, b) for n in ("Regie", "Technik")]))
        if not ok:
            return 1
        time.sleep(1.0)
        check("silent while nobody talks", heard(b, 440.0) < 0.005 and heard(a, 660.0) < 0.005,
              f"{heard(b, 440.0):.4f}")

        names = wait(lambda: [p["name"] for p in (line(b, "Regie")["participants"] or [])] == ["human_regie"], 15)
        check("B sees A on the line (under the account name)", names, str(line(b, "Regie")["participants"]))

        a.ptt("Regie", True)
        check("A's talk key is heard by B", wait(lambda: heard(b, 440.0) > 0.02, 8), f"{heard(b, 440.0):.3f}")
        check("only on the line talked into",
              line(b, "Regie")["out_dbfs"] > -45 and line(b, "Technik")["out_dbfs"] < -70,
              f"Regie {line(b, 'Regie')['out_dbfs']:.0f} dB, Technik {line(b, 'Technik')['out_dbfs']:.0f} dB")
        check("A is shown as talking", line(a, "Regie")["talking"] and not line(a, "Technik")["talking"])
        a.ptt("Regie", False)
        check("releasing the key ends it", wait(lambda: heard(b, 440.0) < 0.005, 8), f"{heard(b, 440.0):.4f}")

        a.toggle_latch("Technik")
        check("latch keeps talking without a key", wait(lambda: heard(b, 440.0) > 0.02, 8))
        check("latched line is the Technik line", line(b, "Technik")["out_dbfs"] > -45 and line(b, "Regie")["out_dbfs"] < -70)

        time.sleep(2.0)  # the measuring window must hold only steady tone
        full = heard(b, 440.0)
        b.set_volume("Technik", 50)
        time.sleep(2.0)
        check("volume 50 % halves it", 0.40 < heard(b, 440.0) / max(full, 1e-9) < 0.60,
              f"{heard(b, 440.0) / max(full, 1e-9):.2f}")
        b.set_listen_mute("Technik", True)
        check("listen mute silences the line", wait(lambda: heard(b, 440.0) < 0.005, 6))
        b.set_listen_mute("Technik", False)
        b.set_volume("Technik", 100)
        check("and brings it back", wait(lambda: heard(b, 440.0) > 0.02, 6))

        a.set_global_mute(True)
        check("global mute stops the latched line", wait(lambda: heard(b, 440.0) < 0.005, 8))
        a.set_global_mute(False)
        check("and releasing it resumes", wait(lambda: heard(b, 440.0) > 0.02, 8))
        a.toggle_latch("Technik")
        check("latch off ends it", wait(lambda: heard(b, 440.0) < 0.005, 8))

        b.ptt("Regie", True)
        a.ptt("Technik", True)
        time.sleep(3)
        check("both directions at once, each on its own line",
              heard(a, 660.0) > 0.02 and heard(b, 440.0) > 0.02, f"{heard(a, 660.0):.3f} / {heard(b, 440.0):.3f}")
    finally:
        a.stop()
        b.stop()
    check("stop leaves no line behind", a.hub._lines == () and b.hub._lines == ())
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


sys.exit(main())
