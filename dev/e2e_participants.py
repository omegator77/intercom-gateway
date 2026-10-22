"""End-to-end check of 'who else is on the line' against the local test stack (stack.py up && setup):
the gateway line must see the other participant (and not itself), and notice when that one leaves."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from gateway.audio import AudioCfg, SyntheticLineAudio
from gateway.auth import AuthClient
from gateway.config import LineCfg
from gateway.http import create_client
from gateway.rtc import LineConnection

STATE = json.loads((Path(__file__).parent / ".state" / "state.json").read_text())
BASE = STATE["base"]
assert BASE.startswith("http://127.0.0.1"), "must only talk to the local stack"


async def join(http, who: str, line: str):
    u = STATE["users"][who]
    cfg = LineCfg(name=line, production_id=STATE["productionId"], line_id=STATE["lines"][line],
                  username=u["username"], password=u["password"], tone_hz=440.0)
    token = await AuthClient(http, cfg.username, cfg.password).token()
    audio = SyntheticLineAudio(cfg, AudioCfg("synthetic", 60), asyncio.get_running_loop())
    await audio.start()
    conn = LineConnection(http, token, audio.track, audio.on_remote_pcm)
    await conn.connect(cfg.production_id, cfg.line_id, who)
    return cfg, audio, conn


async def main() -> int:
    http = await create_client(BASE)
    gw_cfg, gw_audio, gw = await join(http, "gw_regie", "Regie")
    results = {}
    try:
        alone = await gw.participants(gw_cfg.production_id, gw_cfg.line_id)
        results["alone on the line: list is empty (own session left out)"] = alone == []
        _, h_audio, human = await join(http, "human_regie", "Regie")
        seen = await gw.participants(gw_cfg.production_id, gw_cfg.line_id)
        print("  gateway sees:", seen)
        results["sees the other participant by name"] = [p["name"] for p in seen] == ["human_regie"]
        results["does not list itself"] = all(p["name"] != "gw_regie" for p in seen)
        await human.close()
        await h_audio.stop()
        # the server only forgets a closed session once it is removed; DELETE /session does that at once
        gone = await gw.participants(gw_cfg.production_id, gw_cfg.line_id)
        results["the participant disappears after leaving"] = all(p["name"] != "human_regie" for p in gone)
    finally:
        await gw.close()
        await gw_audio.stop()
        await http.aclose()
    for name, ok in results.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return 0 if all(results.values()) else 1


sys.exit(asyncio.run(main()))
