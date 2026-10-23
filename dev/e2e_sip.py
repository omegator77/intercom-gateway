"""End-to-end check of the SIP service against the local test stack (stack.py up && setup): a call between two SIP
services in this process (the "OB van" dials, "SAW" answers), with the OB van's lines being REAL intercom lines
reached through the media bridge, and two synthetic people speaking tones on those lines.

  left  = line Regie, both directions     (Regie <-> SAW left)
  right = line Technik, sent to SAW only  (stands in for the commentary line)

Expected: a tone spoken on Regie arrives on SAW's left, a tone spoken on Technik on SAW's right, and the tone SAW
sends on its left is heard on Regie. The SIP accounts only appear on the lines while the call is up."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import numpy as np

from gateway.audio import AudioCfg, Level, MixedPlayout, SyntheticLineAudio, tone_amplitude_near
from gateway.auth import AuthClient
from gateway.config import LineCfg
from gateway.http import create_client
from gateway.rtc import LineConnection
from intercom_sip.calls import CallState, SipService
from intercom_sip.config import parse_sip_config

STATE = json.loads((Path(__file__).parent / ".state" / "state.json").read_text())
BASE = STATE["base"]
assert BASE.startswith("http://127.0.0.1"), "must only talk to the local stack"


class FakeAudio:
    def __init__(self):
        self.out_buf = MixedPlayout(target_ms=60)
        self.out_level = Level()
        self.pushed: list[np.ndarray] = []
        self.track = self

    def push(self, x):
        self.pushed.append(np.array(x, copy=True))


async def feed(binding, freq, level=0.3):
    n, t0 = 0, time.monotonic()
    while True:
        t = (np.arange(480) + n * 480) / 48000
        binding.audio.out_buf.write((level * np.sin(2 * np.pi * freq * t)).astype(np.float32))
        n += 1
        await asyncio.sleep(max(0, t0 + n * 0.01 - time.monotonic()))


def heard(binding, seconds=1.0):
    chunks = binding.audio.pushed
    x = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
    return x[-int(48000 * seconds):]


async def join(http, who: str, line: str, tone: float):
    u = STATE["users"][who]
    cfg = LineCfg(name=line, production_id=STATE["productionId"], line_id=STATE["lines"][line],
                  username=u["username"], password=u["password"], tone_hz=tone)
    token = await AuthClient(http, cfg.username, cfg.password).token()
    audio = SyntheticLineAudio(cfg, AudioCfg("synthetic", 60), asyncio.get_running_loop())
    await audio.start()
    conn = LineConnection(http, token, audio.track, audio.on_remote_pcm)
    await conn.connect(cfg.production_id, cfg.line_id, who)
    return cfg, audio, conn


def account(who: str, line: str) -> dict:
    u = STATE["users"][who]
    return {"name": line, "production_id": STATE["productionId"], "line_id": STATE["lines"][line],
            "username": u["username"], "password": u["password"]}


async def wait_for(cond, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.1)
    return False


async def main() -> int:
    http = await create_client(BASE)
    results: dict[str, bool] = {}
    saw_cfg = parse_sip_config({
        "server": {"url": BASE}, "sip": {"sip_port": 0, "local_ip": "127.0.0.1", "bind_ip": "127.0.0.1",
                                         "rtp_port_min": 41200, "rtp_port_max": 41240},
        "line": [{"name": n, "production_id": "9", "line_id": "9", "username": "x", "password": "x"}
                 for n in ("SAW-L", "SAW-R")],
        "call": [{"name": "SAW", "mode": "answer", "accept_from": ["127.0.0.1"], "codec": "mp2", "bitrate_kbps": 192,
                  "channel_mode": "dual-mono", "left": {"line": "SAW-L"}, "right": {"receive": "SAW-R"}}]})
    saw = SipService(saw_cfg, http)
    await saw.start()
    saw_run = saw.runners[0]

    async def no_lines(*_):
        return

    saw_run._start_lines = saw_run._stop_lines = no_lines  # the answering side is a plain codec in this test
    for b in saw_run.bindings.values():
        b.audio = FakeAudio()

    me_cfg = parse_sip_config({
        "server": {"url": BASE}, "sip": {"sip_port": 0, "local_ip": "127.0.0.1", "bind_ip": "127.0.0.1",
                                         "rtp_port_min": 41000, "rtp_port_max": 41040},
        "line": [account("gw_regie", "Regie"), account("gw_technik", "Technik")],
        "call": [{"name": "SAW Mainz", "mode": "dial", "target": f"sip:saw@127.0.0.1:{saw.ua.port}", "codec": "mp2",
                  "bitrate_kbps": 192, "channel_mode": "dual-mono", "redial": False,
                  "left": {"line": "Regie"}, "right": {"send": "Technik"}}]})
    me = SipService(me_cfg, http)
    await me.start()
    mine = me.runners[0]

    _, h_reg_audio, h_reg = await join(http, "human_regie", "Regie", 1000.0)
    _, h_tec_audio, h_tec = await join(http, "human_technik", "Technik", 3000.0)
    pid = STATE["productionId"]
    try:
        results["before the call the SIP accounts are not on the lines"] = all(
            p["name"] != "gw_regie" for p in await h_reg.participants(pid, STATE["lines"]["Regie"]))
        run = asyncio.gather(me.run(), saw.run())
        results["the call comes up"] = await wait_for(
            lambda: mine.state == CallState.CONNECTED and saw_run.state == CallState.CONNECTED, 15)
        results["both lines are joined while the call is up"] = await wait_for(
            lambda: len(mine.workers) == 2 and all(w.state.value == "connected" for w in mine.workers.values()), 30)
        peer_left = asyncio.ensure_future(feed(saw_run.bindings["SAW-L"], 2000.0))  # SAW's left channel carries 2 kHz out
        t0 = time.monotonic()
        await asyncio.sleep(5.0)
        t1 = time.monotonic()
        reg_people = await h_reg.participants(pid, STATE["lines"]["Regie"])
        results["the SIP account shows up on the line"] = any(p["name"] == "gw_regie" for p in reg_people)
        left, right = heard(saw_run.bindings["SAW-L"], 2.0), heard(saw_run.bindings["SAW-R"], 2.0)
        print(f"  SAW left : 1 kHz {tone_amplitude_near(left, 1000):.3f}  3 kHz {tone_amplitude_near(left, 3000):.3f}")
        print(f"  SAW right: 1 kHz {tone_amplitude_near(right, 1000):.3f}  3 kHz {tone_amplitude_near(right, 3000):.3f}")
        results["Regie's tone arrives on SAW's left"] = (tone_amplitude_near(left, 1000) > 0.03
                                                         and tone_amplitude_near(left, 3000) < 0.01)
        results["Technik's tone arrives on SAW's right"] = (tone_amplitude_near(right, 3000) > 0.03
                                                            and tone_amplitude_near(right, 1000) < 0.01)
        on_regie = h_reg_audio.heard(t1 - 2.0, t1)
        print(f"  Regie hears: 2 kHz {tone_amplitude_near(on_regie, 2000):.3f}  1 kHz {tone_amplitude_near(on_regie, 1000):.3f}")
        results["SAW's left tone is heard on Regie"] = tone_amplitude_near(on_regie, 2000) > 0.03
        print("  media:", json.dumps(mine.status()["media"]))
        peer_left.cancel()
        me.stop()
        results["the SIP accounts leave the lines after the call"] = await wait_for(lambda: mine.workers == {}, 30)
        reg_people = await h_reg.participants(pid, STATE["lines"]["Regie"])
        results["... and are gone from the participant list"] = all(p["name"] != "gw_regie" for p in reg_people)
        saw.stop()
        await asyncio.wait_for(run, 20)
    finally:
        for c, a in ((h_reg, h_reg_audio), (h_tec, h_tec_audio)):
            await c.close()
            await a.stop()
        await me.close()
        await saw.close()
        await http.aclose()
    for name, ok in results.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return 0 if all(results.values()) else 1


sys.exit(asyncio.run(main()))
