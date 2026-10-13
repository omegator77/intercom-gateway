"""End-to-end check of the gateway against the local test stack (python dev/stack.py up && setup).

Synthetic audio only - no sound card is opened. Two gateway lines (Regie, Technik) and one simulated
human per line, each sending its own tone. Verifies: both lines connect, every listener hears exactly
the other side of ITS line (no own voice, no bleed between lines), and that the gateway recovers from
a dropped peer connection and from the server forgetting the session, and cleans up on shutdown.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

import httpx

import gateway.session as session_mod
from gateway.audio import SyntheticLineAudio, make_line_audio, tone_amplitude_near
from gateway.auth import AuthClient
from gateway.config import AudioCfg, LineCfg, ServerCfg
from gateway.http import create_client
from gateway.rtc import LineConnection
from gateway.session import LineWorker, State

STATE = json.loads((Path(__file__).parent / ".state" / "state.json").read_text())
BASE = STATE["base"]
assert BASE.startswith("http://127.0.0.1"), "e2e must only talk to the local stack"

PRESENT, ABSENT = 0.02, 0.005  # tone amplitude thresholds (full scale = 1.0; sent tones are 0.25)
TONES = {"gw_regie": 440.0, "human_regie": 880.0, "gw_technik": 660.0, "human_technik": 1320.0}
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {detail}")


def line_cfg(who: str, line: str) -> LineCfg:
    u = STATE["users"][who]
    return LineCfg(name=line, production_id=STATE["productionId"], line_id=STATE["lines"][line],
                   username=u["username"], password=u["password"], tone_hz=TONES[who])


class Human:
    def __init__(self, who: str, line: str):
        self.who, self.line = who, line

    async def join(self) -> None:
        cfg = line_cfg(self.who, self.line)
        token = await AuthClient(HTTP, cfg.username, cfg.password).token()
        self.audio = SyntheticLineAudio(cfg, AudioCfg("synthetic", 60), asyncio.get_running_loop())
        await self.audio.start()
        self.conn = LineConnection(HTTP, token, self.audio.track, self.audio.on_remote_pcm)
        await self.conn.connect(cfg.production_id, cfg.line_id, self.who)

    async def leave(self) -> None:
        await self.conn.close()
        await self.audio.stop()


def amp(audio: SyntheticLineAudio, freq: float, seconds: float = 3.0) -> float:
    now = time.monotonic()
    return tone_amplitude_near(audio.heard(now - seconds, now), freq)


PHASE = ["start"]
STALLS: list[tuple[float, float, str]] = []  # (seconds since start, lag ms, phase)


async def loop_monitor(t_start: float) -> None:
    """Records every event-loop stall > 40 ms together with what the test was doing at the time."""
    while True:
        t = time.monotonic()
        await asyncio.sleep(0.01)
        lag = (time.monotonic() - t - 0.01) * 1000
        if lag > 40:
            STALLS.append((t - t_start, lag, PHASE[0]))


def active_fraction(audio: SyntheticLineAudio, seconds: float = 3.0) -> float:
    """Share of the window that is not digital silence (separates 'quiet' from 'gappy')."""
    now = time.monotonic()
    x = audio.heard(now - seconds, now)
    return float(np.mean(np.abs(x) > 1e-4)) if len(x) else 0.0


async def observe(seconds: float, auds: dict[str, SyntheticLineAudio]) -> None:
    """Print a time series: event-loop lag and playout stats every 0.5 s."""
    print("   t   loop-lag  " + "  ".join(f"{n}(fill/under/over/drop)" for n in auds))
    t0 = time.monotonic()
    worst = 0.0
    while time.monotonic() - t0 < seconds:
        t1 = time.monotonic()
        await asyncio.sleep(0.5)
        lag = (time.monotonic() - t1 - 0.5) * 1000
        worst = max(worst, lag)
        cols = []
        for a in auds.values():
            s = a.stats()
            cols.append(f"{s['fill_ms']:5.0f}/{s['underruns']}/{s['overruns']}/{s['in_dropped_samples'] // 480}")
        print(f"  {time.monotonic() - t0:4.1f}  {lag:6.0f} ms  " + "   ".join(cols))
    print(f"  worst event-loop lag: {worst:.0f} ms")


import numpy as np  # noqa: E402


async def wait_for(pred, timeout: float, step: float = 0.2) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(step)
    return False


def listens(label: str, audio: SyntheticLineAudio, present: str, absent: list[str]) -> None:
    got = amp(audio, TONES[present])
    check(f"{label} hears {present} ({TONES[present]:.0f} Hz)", got > PRESENT,
          f"amp {got:.3f} (expected ~0.09), non-silent {active_fraction(audio):.0%} of the last 3 s")
    for other in absent:
        a = amp(audio, TONES[other])
        check(f"{label} does NOT hear {other} ({TONES[other]:.0f} Hz)", a < ABSENT, f"amp {a:.4f}")


HTTP: httpx.AsyncClient  # shared client, created once in main()


async def main() -> int:
    global HTTP
    session_mod.HEARTBEAT_S = 2.0  # notice a lost server session quickly
    asyncio.get_running_loop().slow_callback_duration = 0.05
    HTTP = await create_client(BASE)
    server = ServerCfg(BASE)
    audio_cfg = AudioCfg("synthetic", 60)
    workers = {w: LineWorker(line_cfg(w, line), server, audio_cfg, make_line_audio, HTTP)
               for w, line in (("gw_regie", "Regie"), ("gw_technik", "Technik"))}
    t_start = time.monotonic()
    monitor = asyncio.ensure_future(loop_monitor(t_start))
    tasks = [asyncio.ensure_future(w.run()) for w in workers.values()]
    humans = {w: Human(w, line) for w, line in (("human_regie", "Regie"), ("human_technik", "Technik"))}

    try:
        PHASE[0] = "gateways connecting"
        print("\n== connect")
        ok = await wait_for(lambda: all(w.state == State.CONNECTED for w in workers.values()), 25)
        check("both gateway lines reach CONNECTED", ok, str({n: w.state.value for n, w in workers.items()}))
        if not ok:
            return 1
        for n, h in humans.items():
            PHASE[0] = f"{n} joining"
            await h.join()
        PHASE[0] = "steady audio"
        await observe(6, {"gwR": workers["gw_regie"].audio, "huR": humans["human_regie"].audio,
                          "gwT": workers["gw_technik"].audio, "huT": humans["human_technik"].audio})

        print("\n== audio routing (each listener: exactly the other side of its own line)")
        listens("human_regie  ", humans["human_regie"].audio, "gw_regie", ["human_regie", "gw_technik", "human_technik"])
        listens("gw Regie     ", workers["gw_regie"].audio, "human_regie", ["gw_regie", "gw_technik", "human_technik"])
        listens("human_technik", humans["human_technik"].audio, "gw_technik", ["human_technik", "gw_regie", "human_regie"])
        listens("gw Technik   ", workers["gw_technik"].audio, "human_technik", ["gw_technik", "gw_regie", "human_regie"])
        s = workers["gw_regie"].audio.stats()
        check("no playout underruns/overruns on Regie", s["underruns"] == 0 and s["overruns"] == 0, str(s))

        PHASE[0] = "recovery A"
        print("\n== recovery: peer connection dropped on the gateway side (Technik)")
        w = workers["gw_technik"]
        before, old_session = w.sessions_started, w.session_id
        try:
            await asyncio.wait_for(w.conn.pc.close(), 10)  # drop the peer connection under the worker's feet
        except asyncio.TimeoutError:
            check("pc.close() returns within 10 s", False, "aiortc close() hung")
        ok = await wait_for(lambda: w.sessions_started == before + 1 and w.state == State.CONNECTED, 30)
        check("Technik reconnects with a new session", ok and w.session_id != old_session, f"state {w.state.value}")
        await asyncio.sleep(5)
        check("Technik audio flows again", amp(humans["human_technik"].audio, TONES["gw_technik"], 2.5) > PRESENT)

        PHASE[0] = "recovery B"
        print("\n== recovery: server forgets the session (Regie)")
        w = workers["gw_regie"]
        before, old_session = w.sessions_started, w.session_id
        token = await w.auth.token()
        r = await HTTP.delete(f"/api/v1/session/{old_session}", headers={"Cookie": f"auth_token={token}"})
        check("test deleted the session server-side", r.status_code == 200, f"HTTP {r.status_code}")
        ok = await wait_for(lambda: w.sessions_started == before + 1 and w.state == State.CONNECTED, 40)
        check("Regie notices (heartbeat 410) and reconnects", ok and w.session_id != old_session, f"state {w.state.value}")
        await asyncio.sleep(5)
        check("Regie audio flows again", amp(humans["human_regie"].audio, TONES["gw_regie"], 2.5) > PRESENT)

        PHASE[0] = "shutdown"
        print("\n== graceful shutdown")
        last = {n: w.session_id for n, w in workers.items()}
        for w in workers.values():
            w.stop()
        done, pending = await asyncio.wait(tasks, timeout=15)
        check("workers stop within 15 s", not pending and all(w.state == State.STOPPED for w in workers.values()))
        for n, sid in last.items():
            r = await HTTP.get(f"/api/v1/heartbeat/{sid}")
            check(f"{n}: session removed from the server on shutdown", r.status_code == 410, f"heartbeat HTTP {r.status_code}")
    finally:
        for w in workers.values():
            w.stop()
        for h in humans.values():
            try:
                await h.leave()
            except Exception:
                pass
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 20)
        except asyncio.TimeoutError:
            check("workers finished cleanly", False, "worker tasks still running 20 s after stop()")
        await HTTP.aclose()

    monitor.cancel()
    print(f"\nevent-loop stalls > 40 ms: {len(STALLS)}")
    for t, lag, ph in STALLS[:25]:
        print(f"  t={t:5.1f}s  {lag:5.0f} ms  during: {ph}")
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    for noisy in ("aiortc", "aioice", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    sys.exit(asyncio.run(main()))
