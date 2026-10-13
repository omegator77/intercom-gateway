"""Local control channel of a running gateway: lets a separate process (the UI, `intercom-gateway
status|stop`) observe and stop it. The gateway itself never depends on a controller being present.

A tiny HTTP/1.0 server on 127.0.0.1 (random port). Port, pid and a random token are written to
<state dir>/control.json; every request must carry the token in `X-Gateway-Token`. Browsers cannot send
that header cross-origin without a preflight (which is not answered), so web pages cannot reach it."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from . import __version__

log = logging.getLogger("gateway.control")

CONTROL_FILE = "control.json"
MAX_REQUEST_BYTES = 8192


class ControlServer:
    def __init__(self, home: Path, status: Callable[[], dict], stop: Callable[[], None], config_path: str = ""):
        self._home = home
        self._status = status
        self._stop = stop
        self._config_path = config_path
        self._token = secrets.token_hex(16)
        self._server: asyncio.AbstractServer | None = None
        self._started = time.time()
        self.port = 0

    @property
    def file(self) -> Path:
        return self._home / CONTROL_FILE

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        self._home.mkdir(parents=True, exist_ok=True)
        info = {"pid": os.getpid(), "port": self.port, "token": self._token, "started_at": self._started,
                "config": self._config_path, "version": __version__}
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(info), encoding="utf-8")
        os.replace(tmp, self.file)  # atomic: a reader never sees a half-written file
        log.info("control channel on 127.0.0.1:%d", self.port)

    async def close(self) -> None:
        if self._server:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), 3)
            except asyncio.TimeoutError:
                pass
        try:  # only remove the file if it is still ours
            if json.loads(self.file.read_text(encoding="utf-8")).get("token") == self._token:
                self.file.unlink()
        except (OSError, ValueError):
            pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            if len(raw) > MAX_REQUEST_BYTES:
                raise ValueError("request too large")
            head, *header_lines = raw.decode("latin-1").split("\r\n")
            method, path, *_ = head.split(" ")
            headers = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in header_lines if l)}
            if not hmac.compare_digest(headers.get("x-gateway-token", ""), self._token):
                code, body = 401, {"error": "bad token"}
            elif method == "GET" and path == "/status":
                code, body = 200, self._status()
            elif method == "POST" and path == "/stop":
                code, body = 202, {"stopping": True}
                asyncio.get_running_loop().call_soon(self._stop)
            else:
                code, body = 404, {"error": "not found"}
        except Exception as e:  # malformed/slow clients must not affect the gateway
            code, body = 400, {"error": type(e).__name__}
        try:
            data = json.dumps(body).encode()
            writer.write(f"HTTP/1.0 {code} X\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\n\r\n".encode() + data)
            await asyncio.wait_for(writer.drain(), 5)
        except Exception:
            pass
        finally:
            writer.close()


# ------------------------------------------------------------------ client side (synchronous, used by UI/CLI)

class ControlError(Exception):
    pass


def read_control(home: Path) -> dict | None:
    try:
        info = json.loads((home / CONTROL_FILE).read_text(encoding="utf-8"))
        int(info["port"]), str(info["token"])
        return info
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _call(info: dict, method: str, path: str, timeout: float) -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:{int(info['port'])}{path}", method=method,
                                 headers={"X-Gateway-Token": str(info["token"])}, data=b"" if method == "POST" else None)
    # No proxies: this is always a loopback call
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ControlError(str(e)) from None


def fetch_status(info: dict, timeout: float = 2.0) -> dict:
    return _call(info, "GET", "/status", timeout)


def request_stop(info: dict, timeout: float = 3.0) -> None:
    _call(info, "POST", "/stop", timeout)


def find_running(home: Path) -> dict | None:
    """The control info of a gateway that actually answers, else None (a leftover file after a crash is ignored)."""
    info = read_control(home)
    if not info:
        return None
    try:
        fetch_status(info, timeout=1.5)
    except ControlError:
        return None
    return info
