"""Login against the manager. The auth cookie is cached and reused for every reconnect: login is
rate-limited (10/min per client address) and all lines of one gateway share that address."""
from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

log = logging.getLogger("gateway.auth")

API = "/api/v1"
TOKEN_TTL_S = 6 * 24 * 3600  # server cookie lives 7 days; renew a day early


class LoginError(Exception):
    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class AuthClient:
    def __init__(self, http: httpx.AsyncClient, username: str, password: str):
        self._http = http
        self._user = username
        self._pw = password
        self._token: str | None = None
        self._issued = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._token = None

    def invalidate_if(self, stale: str) -> None:
        """Forget the token only if it still is `stale`: several lines notice a revoked login at the same moment, and
        the one that logs in again must not be thrown away by the others."""
        if self._token == stale:
            self._token = None

    async def token(self) -> str:
        async with self._lock:
            if self._token is None or time.monotonic() - self._issued > TOKEN_TTL_S:
                self._token = await self._login()
                self._issued = time.monotonic()
            return self._token

    async def _login(self) -> str:
        try:
            r = await self._http.post(f"{API}/auth/login", json={"username": self._user, "password": self._pw})
        except httpx.HTTPError as e:
            raise LoginError(f"cannot reach {self._http.base_url}: {e}") from e
        if r.status_code == 429:
            raise LoginError("login rate-limited", 429, float(r.headers.get("retry-after", 60)))
        if r.status_code in (400, 401, 403):
            raise LoginError(f"login rejected for {self._user!r} (HTTP {r.status_code}) - check username/password", r.status_code)
        if r.status_code != 200:
            raise LoginError(f"login failed: HTTP {r.status_code}", r.status_code)
        # The cookie is Secure+SameSite=Strict. Carry it explicitly instead of relying on a cookie
        # jar, which refuses to send Secure cookies over plain http (local tests).
        m = re.search(r"auth_token=([^;]+)", ",".join(r.headers.get_list("set-cookie")))
        if not m:
            raise LoginError("login response carried no auth_token cookie")
        log.info("logged in as %s", self._user)
        return m.group(1)
