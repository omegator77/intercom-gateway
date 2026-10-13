"""What does the gateway receive AFTER a talker goes silent? A simulated human sends a tone burst and then
digital silence (like a muted browser track); the gateway records level, spectrum and packet sizes.

    python dev/stack.py up && python dev/stack.py setup && python dev/talkspurt_test.py [relay_type]

relay_type: mixed (default, what the gateway uses) or ssrc-rewrite (what browsers use).
"""
from __future__ import annotations

import asyncio
import collections
import json
import sys
import time
from pathlib import Path

import numpy as np

from gateway.audio import SR, SyntheticLineAudio
from gateway.auth import AuthClient
from gateway.config import AudioCfg, LineCfg
from gateway.http import create_client
from gateway.rtc import LineConnection

STATE = json.loads((Path(__file__).parent / ".state" / "state.json").read_text())
assert STATE["base"].startswith("http://127.0.0.1")
RELAY = sys.argv[1] if len(sys.argv) > 1 else "mixed"
NOISE_DB = float(sys.argv[2]) if len(sys.argv) > 2 else None  # sender noise floor after the tone (None = digital zeros)
TONE_START, TONE_END, TOTAL = 3.0, 7.0, 24.0
CRCS: list[tuple[float, int, int]] = []


def cfg_for(who: str, line: str, tone: float | None) -> LineCfg:
    u = STATE["users"][who]
    return LineCfg(name=line, production_id=STATE["productionId"], line_id=STATE["lines"][line],
                   username=u["username"], password=u["password"], tone_hz=tone)


class NoisyAudio(SyntheticLineAudio):
    """Like the synthetic sender, but 'silence' is a faint noise floor instead of digital zeros."""

    async def _generate(self) -> None:
        rng = np.random.default_rng(1)
        t0 = time.monotonic()
        n = 0
        while True:
            delay = t0 + n * 0.01 - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if self.tone_hz:
                t = (np.arange(480) + self._phase) / SR
                x = (0.25 * np.sin(2 * np.pi * self.tone_hz * t)).astype(np.float32)
                self._phase += 480
            elif NOISE_DB is not None:
                x = (rng.standard_normal(480) * 10 ** (NOISE_DB / 20)).astype(np.float32)
            else:
                x = np.zeros(480, np.float32)
            self.track.push(x)
            n += 1


async def join(http, who, line, relay, tap=None):
    cfg = cfg_for(who, line, None)
    token = await AuthClient(http, cfg.username, cfg.password).token()
    audio = (NoisyAudio if who.startswith("human") else SyntheticLineAudio)(cfg, AudioCfg("synthetic", 60), asyncio.get_running_loop())
    await audio.start()
    class R:  # minimal recorder: only packet sizes matter here
        def packet(self, n, ts, seq, ssrc, pt, crc=0):
            tap.append((time.monotonic(), n))
            CRCS.append((time.monotonic(), crc, ssrc))

        def rx(self, pcm):
            pass

    conn = LineConnection(http, token, audio.track, audio.on_remote_pcm, relay_type=relay,
                          recorder=R() if tap is not None else None)
    await conn.connect(cfg.production_id, cfg.line_id, who)
    return audio, conn


def db(x):
    return 20 * np.log10(max(float(np.sqrt(np.mean(np.square(x)))) if len(x) else 0.0, 1e-9))


async def main() -> int:
    http = await create_client(STATE["base"])
    packets: list[tuple[float, int]] = []
    gw_audio, gw_conn = await join(http, "gw_regie", "Regie", None if RELAY == "ssrc-rewrite" else RELAY, packets)
    human_audio, human_conn = await join(http, "human_regie", "Regie", None)  # humans behave like browsers
    t0 = time.monotonic()
    print(f"gateway relay={RELAY}; human tone 1000 Hz from {TONE_START}s to {TONE_END}s, silence afterwards")
    print(" sec  rx dBFS  pkt bytes(mean/max)  dominant Hz")
    while time.monotonic() - t0 < TOTAL:
        t = time.monotonic() - t0
        human_audio.tone_hz = 1000.0 if TONE_START <= t < TONE_END else None
        await asyncio.sleep(0.05)
        if int(t * 20) % 20 == 0 and t > 0 and abs(t - round(t)) < 0.05:
            pass
    now = time.monotonic()
    rows = collections.defaultdict(list)
    for ts, n in packets:
        rows[int(ts - t0)].append(n)
    for s in range(int(TOTAL) - 1):
        x = gw_audio.heard(t0 + s, t0 + s + 1)
        if len(x) == 0:
            continue
        spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
        f = np.fft.rfftfreq(len(x), 1 / SR)
        dom = f[int(np.argmax(spec))] if spec.max() > 1e-3 else 0
        sizes = rows.get(s, [0])
        print(f" {s:3d}  {db(x):7.1f}  {np.mean(sizes):8.1f}/{max(sizes):4d}      {dom:7.0f}")
    late = [c for t, c, s in CRCS if t - t0 > 10]
    print(f"packets after 10 s: {len(late)}, distinct payloads: {len(set(late))}, distinct ssrcs: {len({s for t, c, s in CRCS})}")
    for c, a in ((gw_conn, gw_audio), (human_conn, human_audio)):
        await c.close()
        await a.stop()
    await http.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
