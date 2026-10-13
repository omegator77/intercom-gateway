"""Differential check: the browser-style session flow (NO relayType in the request) must behave the same
on production's code (main) and on the relayType branch, against a real SMB.

    python dev/compat_check.py run dev/.state/compat.branch.json
    python dev/compat_check.py compare dev/.state/compat.main.json dev/.state/compat.branch.json

`run` performs: login, GET /auth/me, GET lines, POST /session without relayType, complete the WebRTC
handshake, heartbeat, DELETE. Random values in the SDP offer (ports, fingerprint, ssrc numbers, ids)
are masked so two runs can be compared line by line.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from pathlib import Path

from gateway.audio import SyntheticLineAudio
from gateway.auth import AuthClient
from gateway.config import AudioCfg, LineCfg
from gateway.http import create_client
from gateway.rtc import API, LineConnection

STATE = json.loads((Path(__file__).parent / ".state" / "state.json").read_text())
BASE = STATE["base"]
assert BASE.startswith("http://127.0.0.1"), "local stack only"
logging.getLogger("aiortc").setLevel(logging.WARNING)
logging.getLogger("aioice").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)


UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def mask_uuids(lines: list[str]) -> list[str]:
    """Stream/msid ids are random per session. Idempotent, so it also cleans up older result files."""
    return [UUID.sub("<uuid>", l) for l in lines]


def normalize(sdp: str) -> list[str]:
    out = []
    for line in sdp.splitlines():
        if line.startswith("o="):
            out.append("o=<masked>")
        elif line.startswith(("a=ice-ufrag", "a=ice-pwd")):
            out.append(line.split(":")[0] + ":<masked>")
        elif line.startswith("a=fingerprint"):
            out.append("a=fingerprint:<masked>")
        elif line.startswith("a=candidate"):
            t = line.split()
            out.append(f"a=candidate {t[2]} typ {t[7]}")
        elif line.startswith("a=ssrc:"):
            out.append(re.sub(r"(a=ssrc:)\d+( [a-z]+:).*", r"\1<n>\2<masked>", line))
        elif line.startswith("a=ssrc-group"):
            out.append(re.sub(r"\d+", "<n>", line))
        else:
            out.append(line)
    return mask_uuids(out)


async def run(out_path: str) -> None:
    http = await create_client(BASE)
    u = STATE["users"]["human_regie"]
    pid, lid = STATE["productionId"], STATE["lines"]["Regie"]
    token = await AuthClient(http, u["username"], u["password"]).token()
    hdr = {"Cookie": f"auth_token={token}"}

    facts: dict = {}
    me = (await http.get(f"{API}/auth/me", headers=hdr)).json()
    facts["auth_me"] = {"status_keys": sorted(me), "user_keys": sorted(me["user"]),
                        "memberships": [m["role"] for m in me["memberships"]]}
    lines = (await http.get(f"{API}/production/{pid}/line", headers=hdr)).json()
    facts["lines"] = sorted((l["name"], sorted(l)) for l in lines)

    cfg = LineCfg(name="Regie", production_id=pid, line_id=lid, username=u["username"], password=u["password"])
    audio = SyntheticLineAudio(cfg, AudioCfg("synthetic", 60), asyncio.get_running_loop())
    await audio.start()
    conn = LineConnection(http, token, audio.track, audio.on_remote_pcm, relay_type=None)  # exactly what the browser sends
    try:
        await conn.connect(pid, lid, "Regie")
        facts["handshake"] = {"connected": conn.pc.connectionState == "connected",
                              "heartbeat_status": await conn.heartbeat()}
        offer = conn.offer_sdp
        facts["offer_distinct_ssrcs"] = len({l.split()[0] for l in offer.splitlines() if l.startswith("a=ssrc:")})
        facts["offer_normalized"] = normalize(offer)
    finally:
        await conn.close()
        await audio.stop()

    # Informational, an EXPECTED difference: an unsupported relayType used to be silently ignored.
    r = await http.post(f"{API}/session", json={"productionId": pid, "lineId": lid, "username": "x", "relayType": "bogus"},
                        headers=hdr)
    facts["unsupported_relayType_status"] = r.status_code
    if r.status_code == 201:
        await http.delete(f"{API}/session/{r.json()['sessionId']}", headers=hdr)
    await http.aclose()
    Path(out_path).write_text(json.dumps(facts, indent=1, sort_keys=True))
    print(f"wrote {out_path}: connected={facts['handshake']['connected']}, ssrcs in offer={facts['offer_distinct_ssrcs']}")


def compare(a_path: str, b_path: str) -> int:
    a, b = (json.loads(Path(p).read_text()) for p in (a_path, b_path))
    for facts in (a, b):
        facts["offer_normalized"] = mask_uuids(facts["offer_normalized"])
    expected = {"unsupported_relayType_status"}
    bad = 0
    for key in sorted(set(a) | set(b)):
        same = a.get(key) == b.get(key)
        tag = "same" if same else ("DIFFERENT (expected)" if key in expected else "DIFFERENT")
        if key == "offer_normalized":
            n = len(a[key])
            tag = f"same ({n} SDP lines, identical after masking random values)" if same else "DIFFERENT"
        print(f"  {key:32} {tag}" + ("" if same else f"   main={a.get(key)!r}  branch={b.get(key)!r}" if key != "offer_normalized" else ""))
        if not same and key not in expected:
            bad += 1
            if key == "offer_normalized":
                import difflib
                for l in difflib.unified_diff(a[key], b[key], "main", "branch", lineterm="", n=0):
                    print("      " + l)
    print("\nRESULT:", "identical apart from the expected difference" if not bad else f"{bad} unexpected difference(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    if sys.argv[1] == "run":
        asyncio.run(run(sys.argv[2]))
    else:
        sys.exit(compare(sys.argv[2], sys.argv[3]))
