"""Several people talk at once on one line: the gateway (browser mode, per-SSRC decoding) must play all of
them, and nobody may hear themselves.

    python dev/multitalker_test.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from gateway.audio import SyntheticLineAudio, tone_amplitude_near
from gateway.auth import AuthClient
from gateway.config import AudioCfg, LineCfg
from gateway.http import create_client
from gateway.rtc import LineConnection

STATE = json.loads((Path(__file__).parent / ".state" / "state.json").read_text())
assert STATE["base"].startswith("http://127.0.0.1")
WHO = {"gw_regie": 440.0, "human_regie": 880.0, "gw_technik": 660.0}  # all three join the SAME line (Regie)
failures = 0


def check(name, ok, detail=""):
    global failures
    failures += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {detail}")


async def main() -> int:
    http = await create_client(STATE["base"])
    parts = {}
    for who, hz in WHO.items():
        u = STATE["users"][who]
        cfg = LineCfg("Regie", STATE["productionId"], STATE["lines"]["Regie"], u["username"], u["password"], tone_hz=hz)
        token = await AuthClient(http, cfg.username, cfg.password).token()
        audio = SyntheticLineAudio(cfg, AudioCfg("synthetic", 60), asyncio.get_running_loop())
        await audio.start()
        conn = LineConnection(http, token, audio.track, audio.on_remote_pcm)
        await conn.connect(cfg.production_id, cfg.line_id, who)
        parts[who] = (audio, conn)
    await asyncio.sleep(6)
    now = time.monotonic()
    for who, (audio, conn) in parts.items():
        heard = audio.heard(now - 3, now)
        for other, hz in WHO.items():
            a = tone_amplitude_near(heard, hz)
            if other == who:
                check(f"{who} does NOT hear itself ({hz:.0f} Hz)", a < 0.005, f"amp {a:.4f}")
            else:
                check(f"{who} hears {other} ({hz:.0f} Hz)", a > 0.02, f"amp {a:.3f}")
        s = audio.stats()
        print(f"     {who}: talkers={s['talkers']} underruns={s['underruns']} overruns={s['overruns']} lost={conn.rx_lost}")
    for audio, conn in parts.values():
        await conn.close()
        await audio.stop()
    await http.aclose()
    print("FAILED" if failures else "all passed")
    return 1 if failures else 0


sys.exit(asyncio.run(main()))
