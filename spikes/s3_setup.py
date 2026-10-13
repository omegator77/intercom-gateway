"""Create a production, one line and three throwaway participant accounts in the
LOCAL spike manager (127.0.0.1:8001). Writes state to a scratch JSON file."""
import asyncio
import json
import os
import secrets
import sys

import httpx

BASE = "http://127.0.0.1:8001"
API = "/api/v1"
STATE = sys.argv[1]
ADMIN_PW = os.environ["SPIKE_ADMIN_PW"]


def token_from(resp: httpx.Response) -> str:
    import re
    m = re.search(r"auth_token=([^;]+)", ",".join(resp.headers.get_list("set-cookie")))
    assert m, "no cookie"
    return m.group(1)


async def main():
    assert BASE.startswith("http://127.0.0.1"), "spike must only talk to localhost"
    async with httpx.AsyncClient(base_url=BASE, timeout=20) as c:
        r = await c.post(f"{API}/auth/login", json={"username": "spikeadmin", "password": ADMIN_PW})
        r.raise_for_status()
        c.headers["Cookie"] = f"auth_token={token_from(r)}"

        r = await c.post(f"{API}/production", json={"name": "GatewaySpike", "lines": [{"name": "Regie"}]})
        r.raise_for_status()
        prod = r.json()
        print("production response keys:", list(prod.keys()))
        pid = str(prod.get("productionId") or prod.get("_id"))
        lines = prod.get("lines") or []
        if not lines:
            r = await c.get(f"{API}/production/{pid}/line")
            r.raise_for_status()
            lines = r.json()
        line_id = str(lines[0]["id"])

        users = {}
        for name in ("alice", "bob", "carol"):
            r = await c.post(f"{API}/auth/invite", json={"productionId": int(pid), "role": "participant"})
            r.raise_for_status()
            inv = r.json()
            tok = inv.get("token") or str(inv.get("url") or inv.get("link")).rstrip("/").split("/")[-1]
            pw = secrets.token_urlsafe(14)
            async with httpx.AsyncClient(base_url=BASE, timeout=20) as anon:
                r = await anon.post(f"{API}/auth/invite/{tok}/accept",
                                    json={"username": f"spike_{name}", "password": pw, "displayName": name.title()})
                r.raise_for_status()
            users[name] = {"username": f"spike_{name}", "password": pw}
        json.dump({"productionId": pid, "lineId": line_id, "users": users}, open(STATE, "w"))
        print("setup ok: production", pid, "line", line_id, "users", list(users))


asyncio.run(main())
