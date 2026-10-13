"""Does a talker keep being forwarded for minutes? A sender (like the gateway) plays a constant tone for several
minutes and a second participant (like the phone) logs every 10 s what it still hears.

    python dev/stack.py up && python dev/stack.py setup && python dev/longrun_test.py [seconds] [mixed]
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import numpy as np

from gateway.audio import SyntheticLineAudio, tone_amplitude_near
from gateway.auth import AuthClient
from gateway.config import AudioCfg, LineCfg
from gateway.http import create_client
from gateway.rtc import LineConnection

STATE = json.loads((Path(__file__).parent / ".state" / "state.json").read_text())
assert STATE["base"].startswith("http://127.0.0.1")
TOTAL = float(sys.argv[1]) if len(sys.argv) > 1 else 240.0
RELAY = "mixed" if "mixed" in sys.argv else None


async def join(http, who, hz, relay=None):
    u = STATE["users"][who]
    cfg = LineCfg("Regie", STATE["productionId"], STATE["lines"]["Regie"], u["username"], u["password"], tone_hz=hz)
    token = await AuthClient(http, cfg.username, cfg.password).token()
    audio = SyntheticLineAudio(cfg, AudioCfg("synthetic", 60), asyncio.get_running_loop())
    await audio.start()
    conn = LineConnection(http, token, audio.track, audio.on_remote_pcm, relay_type=relay)
    await conn.connect(cfg.production_id, cfg.line_id, who)
    return audio, conn


async def main() -> int:
    http = await create_client(STATE["base"])
    sender, sender_conn = await join(http, "gw_regie", 440.0, RELAY)  # the "gateway": constant tone, no talking pauses
    listener, listener_conn = await join(http, "human_regie", None)  # the "phone": silent, only listens
    t0 = time.monotonic()
    print(f"sender relay={RELAY or 'ssrc-rewrite'}; listener logs what it hears of the 440 Hz tone")
    print("  sec   440Hz-amp   level-dBFS   listener-talkers")
    last_heard = 0.0
    while time.monotonic() - t0 < TOTAL:
        await asyncio.sleep(10)
        now = time.monotonic()
        x = listener.heard(now - 5, now)
        amp = tone_amplitude_near(x, 440.0)
        db = 20 * np.log10(max(float(np.sqrt(np.mean(np.square(x)))) if len(x) else 0.0, 1e-9))
        talkers = listener.stats()["talkers"]
        if amp > 0.02:
            last_heard = now - t0
        print(f"  {now - t0:4.0f}   {amp:8.3f}   {db:9.1f}   {talkers}", flush=True)
    print(f"last time the tone was heard: {last_heard:.0f} s of {TOTAL:.0f} s")
    for c, a in ((sender_conn, sender), (listener_conn, listener)):
        await c.close()
        await a.stop()
    await http.aclose()
    return 0


sys.exit(asyncio.run(main()))
