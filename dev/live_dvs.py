"""Live check with REAL Dante Virtual Soundcard channels, against the LOCAL test stack (never production).

  line "Regie"  : the gateway plays that line's mix into   DVS Transmit 15-16   (out to the Dante network)
  line "Technik": the gateway captures                     DVS Receive  15-16   (in from the Dante network)
  two simulated humans (synthetic audio): human_regie sends an 880 Hz tone, human_technik listens.

With a Dante Controller loopback  "Receive 15-16 <- Transmit 15-16" (same DVS) the tone has to travel
  human_regie -> Regie line -> gateway -> DVS Tx -> Dante -> DVS Rx -> gateway -> Technik line -> human_technik
Without that subscription the capture side is silent and the script says so.

    python dev/live_dvs.py [--seconds 20] [--tx "DVS Transmit 15-16"] [--rx "DVS Receive 15-16"] [--out-gain -6]

Nothing is opened before the devices have been resolved and printed. Streams are closed on exit.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

from gateway.audio import SyntheticLineAudio, make_line_audio, match_device, tone_amplitude_near
from gateway.auth import AuthClient
from gateway.config import AudioCfg, LineCfg, ServerCfg
from gateway.http import create_client
from gateway.rtc import LineConnection
from gateway.session import LineWorker, State

STATE = json.loads((Path(__file__).parent / ".state" / "state.json").read_text())
BASE = STATE["base"]
assert BASE.startswith("http://127.0.0.1"), "live device test runs against the local stack only"
TONE_HZ = 880.0


def cfg_for(who: str, line: str, **kw) -> LineCfg:
    u = STATE["users"][who]
    return LineCfg(name=line, production_id=STATE["productionId"], line_id=STATE["lines"][line],
                   username=u["username"], password=u["password"], **kw)


async def main(a: argparse.Namespace) -> int:
    # 1. read-only: resolve devices, print, refuse to continue on anything unexpected
    devices, apis = sd.query_devices(), sd.query_hostapis()
    tx = match_device(devices, apis, a.tx, "output")
    rx = match_device(devices, apis, a.rx, "input")
    for kind, idx in (("playback", tx), ("capture ", rx)):
        d = devices[idx]
        print(f"  {kind}: [{idx}] {d['name']}  ({apis[d['hostapi']]['name']}, "
              f"{d['max_output_channels'] or d['max_input_channels']} ch, default {int(d['default_samplerate'])} Hz)")
    print(f"  gateway output gain {a.out_gain:+.0f} dB, tone {TONE_HZ:.0f} Hz, run time {a.seconds:.0f} s\n")

    http = await create_client(BASE)
    server = ServerCfg(BASE)
    real = AudioCfg("portaudio", 60)
    gw_out = LineWorker(cfg_for("gw_regie", "Regie", output_device=a.tx, output_channels=(0, 1), out_gain_db=a.out_gain),
                        server, real, make_line_audio, http)
    gw_in = LineWorker(cfg_for("gw_technik", "Technik", input_device=a.rx, input_channels=(0, 1)),
                       server, real, make_line_audio, http)
    tasks = [asyncio.ensure_future(w.run()) for w in (gw_out, gw_in)]

    humans = []
    ok = False
    try:
        end = time.monotonic() + 20
        while time.monotonic() < end and not all(w.state == State.CONNECTED for w in (gw_out, gw_in)):
            await asyncio.sleep(0.3)
        if not all(w.state == State.CONNECTED for w in (gw_out, gw_in)):
            print("gateway lines did not connect:", {w.cfg.name: (w.state.value, w.last_error) for w in (gw_out, gw_in)})
            return 1
        print("both gateway lines connected, real devices are open\n")

        async def join(who, line, tone):
            c = cfg_for(who, line, tone_hz=tone)
            token = await AuthClient(http, c.username, c.password).token()
            audio = SyntheticLineAudio(c, AudioCfg("synthetic", 60), asyncio.get_running_loop())
            await audio.start()
            conn = LineConnection(http, token, audio.track, audio.on_remote_pcm)
            await conn.connect(c.production_id, c.line_id, who)
            humans.append((audio, conn))
            return audio

        h_regie = await join("human_regie", "Regie", TONE_HZ)  # talks on Regie
        h_technik = await join("human_technik", "Technik", None)  # only listens on Technik

        t0 = time.monotonic()
        print("   t   Regie: out dBFS  fill/under/over        Technik: in dBFS   human_technik hears")
        while time.monotonic() - t0 < a.seconds:
            await asyncio.sleep(2.0)
            now = time.monotonic()
            heard = tone_amplitude_near(h_technik.heard(now - 2.0, now), TONE_HZ)
            s = gw_out.audio.stats()
            print(f"  {now - t0:4.0f}   {gw_out.audio.levels()[1]:8.1f}   {s['fill_ms']:5.0f}/{s['underruns']}/{s['overruns']}"
                  f"          {gw_in.audio.levels()[0]:8.1f}        amp {heard:.3f}")

        now = time.monotonic()
        heard = tone_amplitude_near(h_technik.heard(now - 4.0, now), TONE_HZ)
        s_out, s_in = gw_out.audio.stats(), gw_in.audio.stats()
        in_db = gw_in.audio.levels()[0]
        print()
        print(f"  Regie out : level {gw_out.audio.levels()[1]:.1f} dBFS, underruns {s_out['underruns']}, overruns {s_out['overruns']}")
        print(f"  Technik in: level {in_db:.1f} dBFS, input blocks dropped {s_in['in_dropped_samples'] // 480}, stalls {s_in['in_stalls']}")
        # Silence alone proves nothing: a device that opens but whose clock is not ticking also reads
        # -120 dBFS. Telling those apart needs the callback bookkeeping: a clocked capture device keeps
        # delivering (silent) blocks, a dead one makes the send track time out ("stalls"); a clocked
        # playback device drains the buffer, a dead one makes it overrun over and over.
        capture_clocked = s_in["in_stalls"] < 3
        playback_clocked = s_out["overruns"] < 3
        if not (capture_clocked and playback_clocked):
            dead = [n for n, good in (("capture " + a.rx, capture_clocked), ("playback " + a.tx, playback_clocked)) if not good]
            print(f"\nRESULT: FAIL - the devices opened but are NOT RUNNING ({'; '.join(dead)}): no audio callbacks arrive "
                  "(capture stalls %d, playback overruns %d). Is Dante Virtual Soundcard started and synchronised?"
                  % (s_in["in_stalls"], s_out["overruns"]))
        elif heard > 0.005:
            print(f"\nRESULT: PASS - the tone made the full trip through real Dante channels (human_technik amp {heard:.3f}).")
            ok = True
        elif in_db < -100:
            print("\nRESULT: devices run, but the capture side is silent: Dante has nothing on "
                  f"'{a.rx}'. Subscribe it to '{a.tx}' in Dante Controller (loopback) and run again.")
            ok = True  # device layer fine; routing is the user's side
        else:
            print(f"\nRESULT: FAIL - capture level {in_db:.1f} dBFS but the tone did not arrive on the Technik line.")
        return 0 if ok else 1
    finally:
        for w in (gw_out, gw_in):
            w.stop()
        for audio, conn in humans:
            try:
                await conn.close()
                await audio.stop()
            except Exception:
                pass
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 20)
        except asyncio.TimeoutError:
            print("workers did not stop within 20 s")
        await http.aclose()
        print("devices released.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--tx", default="DVS Transmit 15-16")
    ap.add_argument("--rx", default="DVS Receive 15-16")
    ap.add_argument("--out-gain", type=float, default=-6.0)
    args = ap.parse_args()
    if not 5 <= args.seconds <= 120:
        sys.exit("--seconds must be between 5 and 120")
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    sys.exit(asyncio.run(main(args)))
