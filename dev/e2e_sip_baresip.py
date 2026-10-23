"""Interoperability check against an INDEPENDENT SIP / RTP implementation: baresip (with its Layer II module) in a Docker
container calls our SIP service, which answers, joins real lines of the local test stack, and two synthetic people
speak tones on those lines.

  baresip plays a stereo file (left 1 kHz, right 3 kHz)   ->  must be heard on Regie as 1 kHz
  human_regie speaks 2 kHz, human_technik speaks 4 kHz     ->  baresip must record 2 kHz on its left, 4 kHz on its right

Needs: the local stack (stack.py up && setup), Docker, the image `bs-test` (Debian + baresip) and the config folder
given as argument (see the README of the SIP service, section 'Testing against baresip')."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

from gateway.audio import AudioCfg, SyntheticLineAudio, tone_amplitude_near
from gateway.auth import AuthClient
from gateway.config import LineCfg
from gateway.http import create_client
from gateway.rtc import LineConnection
from intercom_sip.calls import CallState, SipService
from intercom_sip.config import parse_sip_config

STATE = json.loads((Path(__file__).parent / ".state" / "state.json").read_text())
BASE = STATE["base"]
assert BASE.startswith("http://127.0.0.1"), "must only talk to the local stack"
WORK = Path(sys.argv[1]).resolve()  # folder with cfg/ (baresip config + accounts), lr.wav and out/
HOST_IP = sys.argv[2] if len(sys.argv) > 2 else "192.168.199.174"  # an address of this PC that the container can reach
CODEC = sys.argv[3] if len(sys.argv) > 3 else "mp2"


def account(who: str, line: str) -> dict:
    u = STATE["users"][who]
    return {"name": line, "production_id": STATE["productionId"], "line_id": STATE["lines"][line],
            "username": u["username"], "password": u["password"]}


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


async def wait_for(cond, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.1)
    return False


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        raw = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768.0
        return raw.reshape(-1, w.getnchannels()), w.getframerate()


def amp(x: np.ndarray, freq: float, sr: int) -> float:
    """Amplitude of a sine at `freq` in a recording made at `sr` (the recordings are at the codec's own rate)."""
    n = len(x)
    if n < sr // 10:
        return 0.0
    w = np.hanning(n)
    spectrum = np.abs(np.fft.rfft(x.astype(np.float64) * w))
    bins = np.fft.rfftfreq(n, 1.0 / sr)
    band = (bins >= freq * 0.99) & (bins <= freq * 1.01)
    return float(2 * spectrum[band].max() / np.sum(w)) if band.any() else 0.0


async def main() -> int:
    results: dict[str, bool] = {}
    http = await create_client(BASE)
    spec = {"mp2": dict(codec="mp2", bitrate_kbps=192, channel_mode="dual-mono"),
            "g722": dict(codec="g722"), "pcmu": dict(codec="pcmu")}[CODEC]
    cfg = parse_sip_config({
        "server": {"url": BASE},
        "sip": {"sip_port": 5080, "local_ip": HOST_IP, "rtp_port_min": 41000, "rtp_port_max": 41040},
        "line": [account("gw_regie", "Regie"), account("gw_technik", "Technik")],
        "call": [{"name": "baresip", "mode": "answer", "rtp_latch": True, "left": {"line": "Regie"},
                  "right": {"send": "Technik"} if CODEC == "mp2" else {},
                  "accept_from": ["172.17.0.1", "172.17.0.2", "172.17.0.3", "172.17.0.4", "192.168.65.1", "192.168.65.3",
                                  "192.168.199.174", "192.168.199.154", "172.26.80.1", "127.0.0.1"], **spec}]})
    svc = SipService(cfg, http)
    await svc.start()
    runner = svc.runners[0]
    _, h_reg_audio, h_reg = await join(http, "human_regie", "Regie", 2000.0)
    _, h_tec_audio, h_tec = await join(http, "human_technik", "Technik", 4000.0)
    run = asyncio.ensure_future(svc.run())
    out = WORK / "out"
    for f in out.glob("*.wav"):
        f.unlink()
    subprocess.run(["docker", "rm", "-f", "bs2"], capture_output=True)
    codec_arg = {"mp2": "MPA/48000/2", "g722": "G722/16000/1", "pcmu": "PCMU/8000/1"}[CODEC]
    (WORK / "cfg" / "accounts").write_text(
        f"<sip:test@127.0.0.1>;regint=0;answermode=auto;audio_codecs={codec_arg}\n", encoding="utf-8")
    try:
        assert await wait_for(lambda: runner.state == CallState.WAITING, 5)
        t_start = time.monotonic()
        subprocess.run(["docker", "run", "-d", "--name", "bs2", "-v", f"{WORK / 'cfg'}:/root/.baresip",
                        "-v", f"{WORK}:/in", "-v", f"{out}:/out", "-w", "/out", "bs-test", "baresip", "-f", "/root/.baresip",
                        "-e", f"/dial sip:svc@{HOST_IP}:5080", "-t", "20"], check=True, capture_output=True)
        results["baresip's call is accepted and comes up"] = await wait_for(lambda: runner.state == CallState.CONNECTED, 15)
        print("  codec:", runner.media.fmt.describe() if runner.media else None)
        await asyncio.sleep(6.0)
        t1 = time.monotonic()
        st = runner.media.stats() if runner.media else {}
        print("  media:", json.dumps(st))
        on_regie = h_reg_audio.heard(t1 - 3.0, t1)
        print(f"  Regie hears baresip: 1 kHz {tone_amplitude_near(on_regie, 1000):.3f}  3 kHz {tone_amplitude_near(on_regie, 3000):.3f}")
        results["baresip's left channel is heard on Regie"] = (tone_amplitude_near(on_regie, 1000) > 0.03)
        results["no packets lost, nothing undecodable"] = st.get("rx_lost", 1) == 0 and st.get("decode_errors", 1) == 0
        # let baresip hang up (-t) or hang up ourselves, then read what it recorded
        svc.stop()
        await asyncio.wait_for(run, 30)
        subprocess.run(["docker", "stop", "-t", "3", "bs2"], capture_output=True)
        dumps = sorted(out.glob("dump-*-dec.wav"))  # baresip's sndfile module: what it decoded from our stream
        rec = dumps[-1] if dumps else out / "none.wav"
        if rec.exists() and rec.stat().st_size > 1000:
            x, sr = read_wav(rec)
            x = x[len(x) // 4:]
            print(f"  baresip recorded {len(x) / sr:.1f} s at {sr} Hz, {x.shape[1]} channel(s)")
            left = amp(x[:, 0], 2000, sr)
            print(f"  left : 2 kHz {left:.3f}  4 kHz {amp(x[:, 0], 4000, sr):.3f}")
            results["Regie's tone is on baresip's left"] = left > 0.03 and amp(x[:, 0], 4000, sr) < 0.01
            if x.shape[1] >= 2:
                print(f"  right: 2 kHz {amp(x[:, 1], 2000, sr):.3f}  4 kHz {amp(x[:, 1], 4000, sr):.3f}")
                if CODEC == "mp2":
                    results["Technik's tone is on baresip's right"] = amp(x[:, 1], 4000, sr) > 0.03 and amp(x[:, 1], 2000, sr) < 0.01
        else:
            results["baresip recorded what we sent"] = False
        print(subprocess.run(["docker", "logs", "bs2"], capture_output=True, text=True).stdout[-900:])
    finally:
        svc.stop()
        subprocess.run(["docker", "rm", "-f", "bs2"], capture_output=True)
        for c, a in ((h_reg, h_reg_audio), (h_tec, h_tec_audio)):
            await c.close()
            await a.stop()
        await svc.close()
        await http.aclose()
    for name, ok in results.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return 0 if all(results.values()) else 1


sys.exit(asyncio.run(main()))
