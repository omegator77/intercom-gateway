"""Registration at a SIP registrar (RFC 3261 chapter 10): keeps `user@domain` bound to our address for as long as the
service runs, refreshes it before it expires and removes it again when the service stops."""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from dataclasses import dataclass

from .sip import (SipEndpoint, SipError, SipMessage, USER_AGENT, digest_authorization, new_branch, new_tag,
                  parse_challenge, parse_uri)

log = logging.getLogger("sip.register")

RETRY_MIN_S = 10.0
RETRY_MAX_S = 120.0


@dataclass(frozen=True)
class RegisterCfg:
    registrar: str  # sip:host[:port] the REGISTER goes to
    user: str  # the extension / account name
    password: str
    domain: str | None = None  # the "@domain" of the address; default: the registrar's host
    auth_user: str | None = None  # name for the digest login if it differs from `user`
    expires_s: int = 300
    name: str = ""  # what the config calls this account
    contact_user: str | None = None  # user part of the address we register (incoming calls name it); default: `user`


class Registration:
    def __init__(self, ua: SipEndpoint, cfg: RegisterCfg):
        self.ua = ua
        self.cfg = cfg
        self.state = "idle"  # idle | registered | failed | stopped
        self.last_error: str | None = None
        self.granted_s = 0
        self.registered_at: float | None = None
        self._stop = asyncio.Event()
        self._uri = parse_uri(cfg.registrar)
        self.domain = cfg.domain or self._uri.host
        self.contact_user = cfg.contact_user or cfg.user
        self._call_id: str | None = None
        self._tag = new_tag()
        self._cseq = 0

    @property
    def address(self) -> str:
        return f"sip:{self.cfg.user}@{self.domain}"

    def stop(self) -> None:
        self._stop.set()

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def run(self) -> None:
        retry = RETRY_MIN_S
        while not self._stop.is_set():
            try:
                self.granted_s = await self.register(self.cfg.expires_s)
                self.state, self.last_error = "registered", None
                self.registered_at = time.monotonic()
                retry = RETRY_MIN_S
                log.info("registered as %s (%d s)", self.address, self.granted_s)
                await self._sleep(max(10.0, self.granted_s * 0.6))
            except asyncio.CancelledError:
                raise
            except (SipError, OSError) as e:
                self.state, self.last_error = "failed", str(e)
                log.warning("registration as %s failed: %s - trying again in %.0f s", self.address, e, retry)
                await self._sleep(retry)
                retry = min(retry * 2, RETRY_MAX_S)
        if self.state == "registered":
            try:
                await self.register(0)
                log.info("registration as %s removed", self.address)
            except (SipError, OSError) as e:
                log.debug("removing the registration failed: %s", e)
        self.state = "stopped"

    async def register(self, expires: int) -> int:
        """One REGISTER (with the digest round trip if asked for). Returns the granted lifetime in seconds."""
        addr = await self.ua.resolve(self._uri.host, self._uri.port)
        ip = self.ua.advertise_ip or self.ua.local_ip_for(addr)
        if self._call_id is None:
            self._call_id = f"{secrets.token_hex(8)}@{ip}"
        authorization: tuple[str, str] | None = None
        for attempt in range(2):
            self._cseq += 1
            reg_uri = f"sip:{self.domain}"
            msg = SipMessage(f"REGISTER {reg_uri} SIP/2.0")
            msg.add("via", self.ua.via(ip, new_branch()))
            msg.add("max-forwards", "70")
            msg.add("from", f"<{self.address}>;tag={self._tag}")
            msg.add("to", f"<{self.address}>")
            msg.add("call-id", self._call_id)
            msg.add("cseq", f"{self._cseq} REGISTER")
            msg.add("contact", f"<sip:{self.contact_user}@{ip}:{self.ua.port}>;expires={expires}")
            msg.add("expires", str(expires))
            msg.add("user-agent", USER_AGENT)
            if authorization:
                msg.add(*authorization)
            resp = await self.ua.transact(msg, addr)
            if resp.status in (401, 407) and attempt == 0:
                if not self.cfg.password:
                    raise SipError(resp.status, "the registrar wants a password and none is configured")
                header = "www-authenticate" if resp.status == 401 else "proxy-authenticate"
                chal = parse_challenge(resp.get(header) or "")
                value = digest_authorization(chal, self.cfg.auth_user or self.cfg.user, self.cfg.password,
                                             "REGISTER", reg_uri)
                authorization = ("authorization" if resp.status == 401 else "proxy-authorization", value)
                continue
            if resp.status != 200:
                raise SipError(resp.status, resp.reason or "registration refused")
            return self._granted(resp, ip, expires)
        raise SipError(403, "the registrar did not accept the password")

    def _granted(self, resp: SipMessage, ip: str, asked: int) -> int:
        for c in resp.get_list("contact"):
            if f"{ip}:{self.ua.port}" in c:
                m = re.search(r"expires=(\d+)", c)
                if m:
                    return int(m.group(1))
        h = resp.get("expires")
        return int(h) if h and h.isdigit() else asked
