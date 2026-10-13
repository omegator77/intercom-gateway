"""Local, isolated test stack for the gateway (never touches the VPS or your normal dev setup).

    python dev/stack.py up       clone the spike branch of intercom-manager next to this repo, start
                                 SMB + Mongo in Docker (127.0.0.1 only) and the manager on :8001
    python dev/stack.py setup    create admin, a production with two lines and test accounts
    python dev/stack.py down     stop everything and delete the throwaway data
    python dev/stack.py status
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "dev" / ".state"
STATE = STATE_DIR / "state.json"
PIDFILE = STATE_DIR / "manager.pid"
MANAGER_SRC = ROOT.parent / "intercom-manager"
MANAGER_DIR = ROOT.parent / "intercom-manager.spike"
BRANCH = "spike/gateway-mixed-relay"

BASE = "http://127.0.0.1:8001"
API = "/api/v1"
SMB_PORT = 18180
SMB_KEY = "devkey"
MONGO_PORT = 27019
CONTAINERS = ("gw-dev-smb", "gw-dev-mongo")
NPM = shutil.which("npm") or "npm"
NODE = shutil.which("node") or "node"


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, text=True, **kw)


def port_in_use(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def manager_env(jwt_secret: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "PORT": "8001",
        "SMB_ADDRESS": f"http://127.0.0.1:{SMB_PORT}",
        "SMB_APIKEY": SMB_KEY,
        "DB_CONNECTION_STRING": f"mongodb://127.0.0.1:{MONGO_PORT}/gateway-dev",
        "PUBLIC_HOST": BASE,
        "JWT_SECRET": jwt_secret,
    })
    return env


def up() -> None:
    for port in (8001, SMB_PORT, MONGO_PORT):
        if port_in_use(port):
            sys.exit(f"port {port} is already in use - is the stack running? (python dev/stack.py down)")
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if not MANAGER_DIR.exists():
        print(f"cloning {BRANCH} -> {MANAGER_DIR.name} (local clone, your intercom-manager checkout is not touched)")
        sh(["git", "clone", "--quiet", "--branch", BRANCH, "--single-branch", str(MANAGER_SRC), str(MANAGER_DIR)])
    # npm writes node_modules/.package-lock.json only when an install finished; an interrupted
    # install leaves a half-filled node_modules behind that must not be trusted.
    if not (MANAGER_DIR / "node_modules" / ".package-lock.json").exists():
        print("npm ci (first run, or the previous install was interrupted) ...")
        sh([NPM, "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=MANAGER_DIR)

    print("starting Mongo + SMB containers (127.0.0.1 only)")
    sh(["docker", "run", "-d", "--name", "gw-dev-mongo", "-p", f"127.0.0.1:{MONGO_PORT}:27017", "mongo:7"],
       stdout=subprocess.DEVNULL)
    sh(["docker", "run", "-d", "--name", "gw-dev-smb", "--cap-add", "SYS_NICE",
        "-e", "UDP_PORT=10000", "-e", "HTTP_PORT=8180", "-e", f"API_KEY={SMB_KEY}", "-e", "IPV4_ADDR=127.0.0.1",
        "-p", f"127.0.0.1:{SMB_PORT}:8180", "-p", "127.0.0.1:10000:10000/udp",
        "eyevinntechnology/wrtc-sfu:v0.5.1"], stdout=subprocess.DEVNULL)

    jwt = secrets.token_urlsafe(24)
    (STATE_DIR / "jwt.secret").write_text(jwt)
    log = open(STATE_DIR / "manager.log", "w")
    flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    p = subprocess.Popen([NODE, "node_modules/ts-node/dist/bin.js", "-T", "src/server.ts"], cwd=MANAGER_DIR,
                         env=manager_env(jwt), stdout=log, stderr=log, creationflags=flags)
    PIDFILE.write_text(str(p.pid))

    for _ in range(90):
        try:
            if httpx.get(f"{BASE}{API}/auth/me", timeout=2).status_code == 401:
                print(f"manager up on {BASE} (pid {p.pid}); next: python dev/stack.py setup")
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    sys.exit(f"manager did not come up, see {STATE_DIR / 'manager.log'}")


def setup() -> None:
    admin_pw = secrets.token_urlsafe(18)
    jwt = (STATE_DIR / "jwt.secret").read_text()
    sh([NODE, "node_modules/ts-node/dist/bin.js", "-T", "src/scripts/bootstrap-admin.ts", "admin", admin_pw, "Dev Admin"],
       cwd=MANAGER_DIR, env=manager_env(jwt), stdout=subprocess.DEVNULL)

    with httpx.Client(base_url=BASE, timeout=20) as c:
        r = c.post(f"{API}/auth/login", json={"username": "admin", "password": admin_pw})
        r.raise_for_status()
        c.headers["Cookie"] = "auth_token=" + re.search(r"auth_token=([^;]+)", ",".join(r.headers.get_list("set-cookie")))[1]
        r = c.post(f"{API}/production", json={"name": "GatewayDev", "lines": [{"name": "Regie"}, {"name": "Technik"}]})
        r.raise_for_status()
        pid = str(r.json()["productionId"])
        lines = {l["name"]: str(l["id"]) for l in c.get(f"{API}/production/{pid}/line").json()}

        users = {}
        for name in ("gw_regie", "gw_technik", "human_regie", "human_technik"):
            inv = c.post(f"{API}/auth/invite", json={"productionId": int(pid), "role": "participant"})
            inv.raise_for_status()
            token = inv.json()["token"]
            pw = secrets.token_urlsafe(14)
            with httpx.Client(base_url=BASE, timeout=20) as anon:
                anon.post(f"{API}/auth/invite/{token}/accept",
                          json={"username": name, "password": pw, "displayName": name}).raise_for_status()
            users[name] = {"username": name, "password": pw}
    STATE.write_text(json.dumps({"base": BASE, "productionId": pid, "lines": lines, "users": users}, indent=2))
    print(f"setup ok: production {pid}, lines {lines}, accounts {list(users)}  (state: dev/.state/state.json)")


def down() -> None:
    if PIDFILE.exists():
        pid = PIDFILE.read_text().strip()
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True)
        else:
            subprocess.run(["kill", pid], capture_output=True)
        PIDFILE.unlink()
    subprocess.run(["docker", "rm", "-f", "-v", *CONTAINERS], capture_output=True)
    shutil.rmtree(STATE_DIR, ignore_errors=True)
    print("stack stopped, throwaway data deleted (the intercom-manager.spike clone is kept; delete it yourself if unwanted)")


def status() -> None:
    out = subprocess.run(["docker", "ps", "--filter", "name=gw-dev-", "--format", "{{.Names}}  {{.Status}}"],
                         capture_output=True, text=True).stdout.strip()
    print(out or "no gw-dev containers running")
    print("manager on :8001 ->", "up" if port_in_use(8001) else "down")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["up", "setup", "down", "status"])
    {"up": up, "setup": setup, "down": down, "status": status}[ap.parse_args().cmd]()
