"""The status page of the SIP bridge: accounts, calls with levels and lines, and buttons to call and hang up.

A small HTTP server on the service's own asyncio loop (no extra dependencies). The page only talks to
`/api/status` and `/api/calls/<n>/dial|hangup`. By default it listens on this machine only; for anything else a
password is required (HTTP basic authentication)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import time
from pathlib import Path

from . import __version__
from .calls import SipService
from .config import WebCfg

log = logging.getLogger("sip.web")

MAX_HEADER = 16 * 1024
MAX_BODY = 4 * 1024
REASONS = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
           405: "Method Not Allowed", 409: "Conflict", 413: "Payload Too Large"}
PAGE_FILE = Path(__file__).with_name("web_page.html")


class WebServer:
    def __init__(self, service: SipService, cfg: WebCfg):
        self.service = service
        self.cfg = cfg
        self.port = cfg.port
        self.started = time.monotonic()
        self._server: asyncio.AbstractServer | None = None
        self._page: bytes | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.cfg.host, self.cfg.port)
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("status page on http://%s:%d/%s", "127.0.0.1" if self.cfg.host in ("0.0.0.0", "::") else self.cfg.host,
                 self.port, "" if self.cfg.local_only else "  (password protected)")

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # ------------------------------------------------------------ protocol
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                method, path, headers, body = await asyncio.wait_for(self._read(reader), 10)
            except (asyncio.TimeoutError, ValueError, asyncio.IncompleteReadError, ConnectionError):
                return
            status, ctype, payload, extra = self._route(method, path, headers, body)
            head = [f"HTTP/1.1 {status} {REASONS.get(status, '')}", f"Content-Type: {ctype}",
                    f"Content-Length: {len(payload)}", "Cache-Control: no-store", "Connection: close",
                    "X-Content-Type-Options: nosniff", "Referrer-Policy: no-referrer"] + extra
            writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + payload)
            await writer.drain()
        except ConnectionError:
            pass
        except Exception:
            log.exception("status page request failed")
        finally:
            try:
                writer.close()
            except Exception:
                pass

    @staticmethod
    async def _read(reader: asyncio.StreamReader):
        raw = await reader.readuntil(b"\r\n\r\n")
        if len(raw) > MAX_HEADER:
            raise ValueError("header too large")
        lines = raw.decode("latin-1").split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        length = int(headers.get("content-length", "0") or 0)
        if length > MAX_BODY:
            raise ValueError("body too large")
        body = await reader.readexactly(length) if length else b""
        return method.upper(), path.split("?")[0], headers, body

    # ------------------------------------------------------------ routing
    def _allowed_host(self, host: str) -> bool:
        """A page that is only for this machine answers only to this machine's names (DNS rebinding protection)."""
        if not self.cfg.local_only:
            return True
        return host.lower() in (f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}")

    def _cookie_value(self) -> str:
        return hmac.new(self.cfg.password.encode("utf-8"), b"intercom-sip-status-page", hashlib.sha256).hexdigest()

    def _basic_ok(self, headers: dict) -> bool:
        auth = headers.get("authorization", "")
        if not auth.lower().startswith("basic "):
            return False
        try:
            user, _, password = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(user, self.cfg.user) & hmac.compare_digest(password, self.cfg.password)

    def _cookie_ok(self, headers: dict) -> bool:
        for part in headers.get("cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "sipui" and hmac.compare_digest(value, self._cookie_value()):
                return True
        return False

    @staticmethod
    def _json(status: int, obj) -> tuple[int, str, bytes, list[str]]:
        return status, "application/json", json.dumps(obj).encode("utf-8"), []

    def _route(self, method: str, path: str, headers: dict, body: bytes):
        if not self._allowed_host(headers.get("host", "")):
            return self._json(403, {"error": "unknown host name"})
        extra: list[str] = []
        if self.cfg.password:
            # the page asks for the password once (basic authentication); after that a cookie carries the proof, so
            # the page's own requests need no credentials of their own in any browser
            if self._basic_ok(headers):
                extra = [f"Set-Cookie: sipui={self._cookie_value()}; HttpOnly; SameSite=Strict; Path=/"]
            elif not self._cookie_ok(headers):
                return 401, "text/plain; charset=utf-8", b"password required", ['WWW-Authenticate: Basic realm="SIP bridge"']
        if method == "GET" and path in ("/", "/index.html"):
            if self._page is None:
                self._page = PAGE_FILE.read_bytes()
            return 200, "text/html; charset=utf-8", self._page, extra
        if method == "GET" and path == "/api/status":
            return self._json(200, self.status())
        m = re.match(r"^/api/calls/(\d+)/(dial|hangup)$", path)
        if m:
            if method != "POST":
                return self._json(405, {"error": "POST only"})
            if headers.get("x-requested-with") != "intercom-sip":  # a page of another site cannot set this header
                return self._json(403, {"error": "missing header"})
            index = int(m.group(1))
            if index >= len(self.service.runners):
                return self._json(404, {"error": "no such call"})
            if m.group(2) == "dial":
                try:
                    data = json.loads(body or b"{}")
                except ValueError:
                    return self._json(400, {"error": "ungültige Eingabe"})
                number = str(data.get("number", "")) if isinstance(data, dict) else ""
                problem = self.service.dial(index, number or None)
            else:
                problem = self.service.hangup(index)
            return self._json(409, {"error": problem}) if problem else self._json(200, {"ok": True})
        return self._json(404, {"error": "not found"})

    def status(self) -> dict:
        s = self.service.status()
        s["version"] = __version__
        s["uptime_s"] = round(time.monotonic() - self.started)
        return s
