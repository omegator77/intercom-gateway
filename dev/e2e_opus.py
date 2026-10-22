"""End-to-end check of the stereo / Opus-mode / bitrate line options against the local test stack
(python dev/stack.py up && setup). Synthetic audio, nothing leaves the machine.

A gateway line sends in stereo, Opus "audio" mode at 128 kbit/s; a second participant on the same line
sends a plain mono tone. Both must hear each other, and the server must accept the stream unchanged.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from gateway.audio import AudioCfg, SyntheticLineAudio, tone_amplitude_near
from gateway.auth import AuthClient
from gateway.config import LineCfg
from gateway.http import create_client
from gateway.rtc import LineConnection

STATE = json.loads((Path(__file__).parent / ".state" / "state.json").read_text())
BASE = STATE["base"]
assert BASE.startswith("http://127.0.0.1"), "must only talk to the local stack"


async def join(http, who: str, line: str, tone: float, **opts):
    u = STATE["users"][who]
    cfg = LineCfg(name=line, production_id=STATE["productionId"], line_id=STATE["lines"][line],
                  username=u["username"], password=u["password"], tone_hz=tone, **opts)
    token = await AuthClient(http, cfg.username, cfg.password).token()
    audio = SyntheticLineAudio(cfg, AudioCfg("synthetic", 60), asyncio.get_running_loop())
    await audio.start()
    conn = LineConnection(http, token, audio.track, audio.on_remote_pcm, opus=(cfg.opus_mode, cfg.opus_bitrate_kbps))
    await conn.connect(cfg.production_id, cfg.line_id, who)
    return audio, conn


def heard(audio, freq, seconds=3.0):
    now = time.monotonic()
    return tone_amplitude_near(audio.heard(now - seconds, now), freq)


async def main() -> int:
    http = await create_client(BASE)
    gw, gw_conn = await join(http, "gw_regie", "Regie", 440.0, stereo=True, input_channels=(0, 1),
                             opus_mode="audio", opus_bitrate_kbps=128)
    human, human_conn = await join(http, "human_regie", "Regie", 880.0)
    try:
        await asyncio.sleep(6)
        a, b = heard(human, 440.0), heard(gw, 880.0)
        sent = gw_conn.pc.getSenders()[0]
        enc = sent._RTCRtpSender__encoder
        checks = {
            "participant hears the stereo/128k line (440 Hz)": a > 0.02,
            "stereo line hears the participant (880 Hz)": b > 0.02,
            "the custom encoder is the one in use": type(enc).__name__ == "ConfiguredOpusEncoder",
            "128 kbit/s, audio mode": (enc.bitrate_kbps, enc.mode) == (128, "audio"),
            "stereo frames are sent": gw.track.channels == 2,
        }
        for name, ok in checks.items():
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        print(f"  levels: participant heard {a:.3f}, gateway heard {b:.3f}")
        return 0 if all(checks.values()) else 1
    finally:
        for c, au in ((gw_conn, gw), (human_conn, human)):
            await c.close()
            await au.stop()
        await http.aclose()


sys.exit(asyncio.run(main()))
