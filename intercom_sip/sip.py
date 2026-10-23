"""A small SIP user agent (RFC 3261) over UDP: place a call, accept a call, answer OPTIONS, hang up.

Deliberately narrow - what two broadcast codecs or a PBX need to bring a call up and down:
  * outgoing INVITE with retransmissions, provisional responses, digest authentication, CANCEL, ACK, BYE
  * incoming INVITE (answered by a callback), CANCEL, BYE, OPTIONS, re-INVITE of the same codec
Not included: registration, TCP/TLS, SRTP, forking, early media, transfers. Everything runs on the caller's
asyncio loop; there are no threads.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
import socket
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .net import udp_socket

log = logging.getLogger("sip.ua")

T1 = 0.5  # RFC 3261 timer values: first retransmission interval,
T2 = 4.0  # and the cap for it
INVITE_NO_ANSWER_S = 32.0  # nothing at all came back
USER_AGENT = "Open-Intercom-SIP/0.1"
ALLOW = "INVITE, ACK, CANCEL, BYE, OPTIONS, INFO"
COMPACT = {"i": "call-id", "m": "contact", "f": "from", "t": "to", "v": "via", "l": "content-length",
           "c": "content-type", "k": "supported"}
REASONS = {100: "Trying", 180: "Ringing", 200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
           405: "Method Not Allowed", 408: "Request Timeout", 480: "Temporarily Unavailable",
           481: "Call/Transaction Does Not Exist", 486: "Busy Here", 487: "Request Terminated",
           488: "Not Acceptable Here", 501: "Not Implemented", 603: "Decline"}


class SipError(Exception):
    def __init__(self, status: int, reason: str = ""):
        super().__init__(f"{status} {reason}".strip())
        self.status = status
        self.reason = reason


# ---------------------------------------------------------------- messages

@dataclass
class SipMessage:
    start: str  # "INVITE sip:x SIP/2.0" or "SIP/2.0 200 OK"
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""

    @property
    def is_response(self) -> bool:
        return self.start.startswith("SIP/")

    @property
    def status(self) -> int:
        return int(self.start.split()[1]) if self.is_response else 0

    @property
    def reason(self) -> str:
        return self.start.split(None, 2)[2] if self.is_response and len(self.start.split(None, 2)) > 2 else ""

    @property
    def method(self) -> str:
        return "" if self.is_response else self.start.split()[0].upper()

    @property
    def uri(self) -> str:
        return "" if self.is_response else self.start.split()[1]

    def get(self, name: str, default: str | None = None) -> str | None:
        name = name.lower()
        for k, v in self.headers:
            if k == name:
                return v
        return default

    def get_all(self, name: str) -> list[str]:
        name = name.lower()
        return [v for k, v in self.headers if k == name]

    def get_list(self, name: str) -> list[str]:
        """Every value of a header that may carry several comma separated ones (Record-Route, Via, ...)."""
        out: list[str] = []
        for raw in self.get_all(name):
            out.extend(split_commas(raw))
        return out

    def add(self, name: str, value: str) -> None:
        self.headers.append((name.lower(), value))

    @property
    def cseq(self) -> tuple[int, str]:
        parts = (self.get("cseq") or "0 ").split()
        return (int(parts[0]) if parts and parts[0].isdigit() else 0), (parts[1].upper() if len(parts) > 1 else "")

    @property
    def call_id(self) -> str:
        return self.get("call-id") or ""

    @property
    def branch(self) -> str:
        via = (self.get_list("via") or [""])[0]
        m = re.search(r"branch=([^;,\s]+)", via)
        return m.group(1) if m else ""

    def encode(self) -> bytes:
        lines = [self.start]
        for k, v in self.headers:
            if k != "content-length":
                lines.append(f"{canonical(k)}: {v}")
        lines.append(f"Content-Length: {len(self.body)}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + self.body


def canonical(name: str) -> str:
    special = {"call-id": "Call-ID", "cseq": "CSeq", "www-authenticate": "WWW-Authenticate"}
    return special.get(name, "-".join(p.capitalize() for p in name.split("-")))


def split_commas(value: str) -> list[str]:
    """Split a header value at commas that are not inside <...> or "..."."""
    out, cur, depth, quoted = [], [], 0, False
    for ch in value:
        if ch == '"':
            quoted = not quoted
        elif ch == "<" and not quoted:
            depth += 1
        elif ch == ">" and not quoted:
            depth = max(0, depth - 1)
        if ch == "," and depth == 0 and not quoted:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        out.append(tail)
    return out


def parse_message(data: bytes) -> SipMessage | None:
    head, sep, body = data.partition(b"\r\n\r\n")
    if not sep:
        head, sep, body = data.partition(b"\n\n")
    text = head.decode("utf-8", errors="replace").replace("\r\n", "\n")
    lines = text.split("\n")
    while lines and not lines[0].strip():  # keep-alive CRLFs in front of a message
        lines.pop(0)
    if not lines:
        return None
    start = lines[0].strip()
    if not (start.startswith("SIP/2.0 ") or start.endswith("SIP/2.0")):
        return None
    msg = SipMessage(start)
    last: list[str] | None = None
    for line in lines[1:]:
        if line[:1] in (" ", "\t") and msg.headers:  # folded continuation
            k, v = msg.headers[-1]
            msg.headers[-1] = (k, v + " " + line.strip())
            continue
        name, colon, value = line.partition(":")
        if not colon:
            continue
        name = name.strip().lower()
        msg.headers.append((COMPACT.get(name, name), value.strip()))
    length = msg.get("content-length")
    if length and length.isdigit() and int(length) <= len(body):
        body = body[:int(length)]
    msg.body = body
    return msg


@dataclass
class SipUri:
    user: str | None
    host: str
    port: int

    def __str__(self) -> str:
        return "sip:" + (f"{self.user}@" if self.user else "") + self.host + (f":{self.port}" if self.port != 5060 else "")


def parse_uri(text: str) -> SipUri:
    """`sip:user@host:port`, `<sip:host>`, a bare `host:port` or `Name <sip:...>;tag=x` (the uri part is taken)."""
    m = re.search(r"<([^>]+)>", text)
    uri = m.group(1) if m else text.strip()
    uri = re.sub(r"^sips?:", "", uri)
    uri = uri.split(";")[0].split("?")[0]
    user = None
    if "@" in uri:
        user, _, uri = uri.rpartition("@")
        user = user.split(":")[0]
    m = re.match(r"^(\[[^\]]+\]|[^:]+)(?::(\d+))?$", uri)
    if not m:
        raise SipError(400, f"bad SIP address {text!r}")
    return SipUri(user or None, m.group(1).strip("[]"), int(m.group(2)) if m.group(2) else 5060)


def header_tag(value: str | None) -> str | None:
    m = re.search(r";\s*tag=([^;>\s]+)", value or "")
    return m.group(1) if m else None


def bare_uri(header: str | None, fallback: str = "") -> str:
    """The URI inside a From / To / Contact header value (without display name and parameters of the header)."""
    m = re.search(r"<([^>]+)>", header or "") or re.search(r"(sips?:[^;>\s]+)", header or "")
    return m.group(1) if m else fallback


def new_branch() -> str:
    return "z9hG4bK" + secrets.token_hex(8)


def new_tag() -> str:
    return secrets.token_hex(5)


# ---------------------------------------------------------------- digest authentication (RFC 2617 / 7616, MD5)

def parse_challenge(value: str) -> dict[str, str]:
    body = re.sub(r"^\s*Digest\s+", "", value, flags=re.I)
    return {k.lower(): v1 if v1 else v2 for k, v1, v2 in re.findall(r'(\w+)\s*=\s*(?:"([^"]*)"|([^\s,]+))', body)}


def digest_authorization(challenge: dict[str, str], user: str, password: str, method: str, uri: str,
                         nc: int = 1, cnonce: str | None = None) -> str:
    realm, nonce = challenge.get("realm", ""), challenge.get("nonce", "")
    algorithm = challenge.get("algorithm", "MD5")
    if algorithm.upper() not in ("MD5", ""):
        raise SipError(401, f"unsupported digest algorithm {algorithm}")
    md5 = lambda s: hashlib.md5(s.encode("utf-8")).hexdigest()  # noqa: E731 - protocol-mandated hash, not security
    ha1 = md5(f"{user}:{realm}:{password}")
    ha2 = md5(f"{method}:{uri}")
    qops = [q.strip() for q in challenge.get("qop", "").split(",") if q.strip()]
    parts = [f'username="{user}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"']
    if "auth" in qops:
        cnonce = cnonce or secrets.token_hex(8)
        ncs = f"{nc:08x}"
        response = md5(f"{ha1}:{nonce}:{ncs}:{cnonce}:auth:{ha2}")
        parts += [f'response="{response}"', "algorithm=MD5", "qop=auth", f"nc={ncs}", f'cnonce="{cnonce}"']
    else:
        parts += [f'response="{md5(f"{ha1}:{nonce}:{ha2}")}"', "algorithm=MD5"]
    if challenge.get("opaque"):
        parts.append(f'opaque="{challenge["opaque"]}"')
    return "Digest " + ", ".join(parts)


# ---------------------------------------------------------------- dialogs

class Dialog:
    """One established call, seen from our side."""

    def __init__(self, ua: "SipEndpoint", call_id: str, local_uri: str, remote_uri: str, local_tag: str,
                 remote_tag: str, remote_target: str, route_set: list[str], role: str, local_cseq: int,
                 remote_cseq: int, local_ip: str, remote_addr: tuple[str, int]):
        self.ua = ua
        self.call_id = call_id
        self.local_uri, self.remote_uri = local_uri, remote_uri
        self.local_tag, self.remote_tag = local_tag, remote_tag
        self.remote_target = remote_target
        self.route_set = route_set
        self.role = role  # "uac" (we called) or "uas" (we were called)
        self.local_cseq = local_cseq
        self.remote_cseq = remote_cseq
        self.local_ip = local_ip
        self.remote_addr = remote_addr  # where requests of this dialog go
        self.local_sdp = ""
        self.remote_sdp = ""
        self.closed = asyncio.Event()
        self.closed_by: str | None = None  # "local", "remote", "no ack"
        self.on_remote_sdp: Callable[[str], bool] | None = None  # re-INVITE: return False to refuse (488)
        self._ack: bytes | None = None
        self._acked = asyncio.Event()

    def _request_addr(self) -> tuple[str, int]:
        if self.route_set:
            u = parse_uri(self.route_set[0])
            return u.host, u.port
        u = parse_uri(self.remote_target)
        return u.host, u.port

    def build_request(self, method: str, branch: str | None = None, cseq: int | None = None) -> SipMessage:
        if cseq is None:
            self.local_cseq += 1
            cseq = self.local_cseq
        msg = SipMessage(f"{method} {self.remote_target} SIP/2.0")
        msg.add("via", self.ua.via(self.local_ip, branch or new_branch()))
        msg.add("max-forwards", "70")
        for r in self.route_set:
            msg.add("route", r)
        msg.add("from", f"<{self.local_uri}>;tag={self.local_tag}")
        msg.add("to", f"<{self.remote_uri}>;tag={self.remote_tag}")
        msg.add("call-id", self.call_id)
        msg.add("cseq", f"{cseq} {method}")
        msg.add("user-agent", USER_AGENT)
        return msg

    async def bye(self) -> None:
        """Hang up. Returns once the other side confirmed or the transaction timed out; never raises."""
        if self.closed.is_set():
            return
        self.closed_by = "local"
        self.closed.set()
        self.ua.forget(self)
        try:
            await self.ua.transact(self.build_request("BYE"), self._request_addr(), timeout=6.0)
        except (SipError, OSError, asyncio.TimeoutError):
            pass

    def remote_hung_up(self, how: str = "remote") -> None:
        if not self.closed.is_set():
            self.closed_by = how
            self.closed.set()
        self.ua.forget(self)


@dataclass
class IncomingCall:
    """What the application gets to decide about."""
    call_id: str
    from_uri: str
    remote_ip: str
    sdp: str  # the caller's offer
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class Accept:
    sdp: str


@dataclass
class Reject:
    status: int = 486
    reason: str = ""


InviteHandler = Callable[[IncomingCall], Awaitable["Accept | Reject"]]


# ---------------------------------------------------------------- the endpoint

class SipEndpoint(asyncio.DatagramProtocol):
    def __init__(self, port: int = 5060, user: str = "intercom", display: str = "Open Intercom",
                 advertise_ip: str | None = None, bind_ip: str = "0.0.0.0"):
        self.port = port
        self.user = user
        self.display = display
        self.advertise_ip = advertise_ip
        self.bind_ip = bind_ip
        self.on_invite: InviteHandler | None = None
        self.on_options: Callable[[str], None] | None = None
        self.on_accepted: Callable[[Dialog], None] | None = None  # a call we accepted became a dialog
        self._transport: asyncio.DatagramTransport | None = None
        self._client: dict[str, asyncio.Queue] = {}  # branch -> responses of one client transaction
        self._dialogs: dict[str, Dialog] = {}  # call-id -> dialog
        self._server_tx: dict[tuple, tuple[bytes, tuple[str, int]]] = {}  # last response to a request, for retransmissions
        self._incoming: dict[str, IncomingCall] = {}  # call-id -> invite that is still being decided
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(lambda: self, sock=udp_socket(self.bind_ip, self.port))
        sock = self._transport.get_extra_info("sockname")
        self.port = sock[1]
        log.info("SIP listening on %s:%d (UDP)", sock[0], sock[1])

    async def close(self) -> None:
        for d in list(self._dialogs.values()):
            await d.bye()
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._transport:
            self._transport.close()
            self._transport = None

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.ensure_future(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    def forget(self, dialog: Dialog) -> None:
        self._dialogs.pop(dialog.call_id, None)

    # ------------------------------------------------------------ addressing
    def local_ip_for(self, addr: tuple[str, int]) -> str:
        if self.advertise_ip:
            return self.advertise_ip
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(addr)  # no packet is sent; the OS just picks the interface that would be used
            return s.getsockname()[0]
        finally:
            s.close()

    def via(self, ip: str, branch: str) -> str:
        return f"SIP/2.0/UDP {ip}:{self.port};branch={branch};rport"

    def contact(self, ip: str) -> str:
        return f"<sip:{self.user}@{ip}:{self.port}>"

    @staticmethod
    async def resolve(host: str, port: int) -> tuple[str, int]:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_DGRAM)
        if not infos:
            raise SipError(404, f"cannot resolve {host}")
        return infos[0][4][0], infos[0][4][1]

    def _sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        if self._transport:
            self._transport.sendto(data, addr)

    # ------------------------------------------------------------ datagrams
    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if data.strip() == b"":  # keep-alive
            return
        try:
            msg = parse_message(data)
        except Exception:
            msg = None
        if msg is None:
            log.debug("ignoring %d bytes of garbage from %s", len(data), addr)
            return
        try:
            if msg.is_response:
                self._on_response(msg, addr)
            else:
                self._on_request(msg, addr)
        except Exception:
            log.exception("handling a SIP message from %s failed", addr)

    def error_received(self, exc: Exception) -> None:
        log.debug("SIP socket error: %s", exc)

    # ------------------------------------------------------------ client transactions
    def _on_response(self, msg: SipMessage, addr: tuple[str, int]) -> None:
        q = self._client.get(msg.branch)
        if q is not None:
            q.put_nowait(msg)
            return
        cseq_n, method = msg.cseq
        d = self._dialogs.get(msg.call_id)
        if d is not None and method == "INVITE" and 200 <= msg.status < 300 and d._ack:
            self._sendto(d._ack, d._request_addr())  # our ACK was lost: the 200 came again

    async def transact(self, msg: SipMessage, addr: tuple[str, int], *, invite: bool = False,
                       timeout: float = 32.0, ring_timeout: float = 90.0,
                       provisional: Callable[[SipMessage], None] | None = None) -> SipMessage:
        """Send a request and wait for its final response, retransmitting like RFC 3261 says for UDP."""
        branch = msg.branch
        q: asyncio.Queue = asyncio.Queue()
        self._client[branch] = q
        loop = asyncio.get_running_loop()
        data = msg.encode()
        interval = T1
        deadline = loop.time() + timeout
        got_provisional = False
        try:
            self._sendto(data, addr)
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise SipError(408, "no answer")
                wait = remaining if (invite and got_provisional) else min(interval, remaining)
                try:
                    resp = await asyncio.wait_for(q.get(), wait)
                except asyncio.TimeoutError:
                    if not (invite and got_provisional):
                        self._sendto(data, addr)
                        interval = min(interval * 2, T2)
                    continue
                if resp.status < 200:
                    if not got_provisional and invite:
                        deadline = loop.time() + ring_timeout
                    got_provisional = True
                    if provisional:
                        provisional(resp)
                    continue
                if invite and resp.status >= 300:
                    self._sendto(self._ack_for_error(msg, resp).encode(), addr)
                return resp
        finally:
            self._client.pop(branch, None)

    @staticmethod
    def _ack_for_error(invite: SipMessage, resp: SipMessage) -> SipMessage:
        ack = SipMessage(f"ACK {invite.uri} SIP/2.0")
        for name in ("via", "from", "call-id"):
            ack.add(name, invite.get(name) or "")
        ack.add("max-forwards", "70")
        ack.add("to", resp.get("to") or invite.get("to") or "")
        ack.add("cseq", f"{invite.cseq[0]} ACK")
        return ack

    # ------------------------------------------------------------ outgoing calls
    async def options(self, target: str, timeout: float = 4.0) -> int:
        """Ask the target whether it is there: its status code (any answer means it is reachable)."""
        uri = parse_uri(target)
        addr = await self.resolve(uri.host, uri.port)
        ip = self.local_ip_for(addr)
        msg = SipMessage(f"OPTIONS {uri} SIP/2.0")
        msg.add("via", self.via(ip, new_branch()))
        msg.add("max-forwards", "70")
        msg.add("from", f'"{self.display}" <sip:{self.user}@{ip}>;tag={new_tag()}')
        msg.add("to", f"<{uri}>")
        msg.add("call-id", f"{secrets.token_hex(8)}@{ip}")
        msg.add("cseq", "1 OPTIONS")
        msg.add("contact", self.contact(ip))
        msg.add("accept", "application/sdp")
        msg.add("user-agent", USER_AGENT)
        resp = await self.transact(msg, addr, timeout=timeout)
        return resp.status

    async def invite(self, target: str, sdp: str, *, auth: tuple[str, str] | None = None,
                     progress: Callable[[int, str], None] | None = None, ring_timeout: float = 90.0,
                     caller: str | None = None) -> Dialog:
        """Call `target`; returns the dialog once the call is established and acknowledged."""
        uri = parse_uri(target)
        addr = await self.resolve(uri.host, uri.port)
        ip = self.local_ip_for(addr)
        call_id = f"{secrets.token_hex(8)}@{ip}"
        from_tag = new_tag()
        local_uri = f"sip:{caller or self.user}@{ip}"
        cseq = 1
        challenge_done = False
        authorization: tuple[str, str] | None = None
        while True:
            msg = SipMessage(f"INVITE {uri} SIP/2.0")
            msg.add("via", self.via(ip, new_branch()))
            msg.add("max-forwards", "70")
            msg.add("from", f'"{self.display}" <{local_uri}>;tag={from_tag}')
            msg.add("to", f"<{uri}>")
            msg.add("call-id", call_id)
            msg.add("cseq", f"{cseq} INVITE")
            msg.add("contact", self.contact(ip))
            msg.add("allow", ALLOW)
            msg.add("user-agent", USER_AGENT)
            if authorization:
                msg.add(*authorization)
            msg.add("content-type", "application/sdp")
            msg.body = sdp.encode("utf-8")
            try:
                resp = await self.transact(
                    msg, addr, invite=True, ring_timeout=ring_timeout,
                    provisional=(lambda r: progress(r.status, r.reason)) if progress else None)
            except asyncio.CancelledError:
                self._spawn(self._cancel(msg, addr))
                raise
            if resp.status in (401, 407) and auth and not challenge_done:
                challenge_done = True
                hdr = "www-authenticate" if resp.status == 401 else "proxy-authenticate"
                chal = parse_challenge(resp.get(hdr) or "")
                value = digest_authorization(chal, auth[0], auth[1], "INVITE", str(uri))
                authorization = ("authorization" if resp.status == 401 else "proxy-authorization", value)
                cseq += 1
                continue
            if not 200 <= resp.status < 300:
                raise SipError(resp.status, resp.reason)
            break
        remote_target = bare_uri((resp.get_list("contact") or [""])[0], str(uri)).split(";")[0]
        routes = list(reversed(resp.get_list("record-route")))
        d = Dialog(self, call_id, local_uri, bare_uri(resp.get("to"), str(uri)).split(";")[0], from_tag,
                   header_tag(resp.get("to")) or "", remote_target, routes, "uac", cseq, 0, ip, addr)
        d.remote_sdp = resp.body.decode("utf-8", errors="replace")
        d.local_sdp = sdp
        self._dialogs[call_id] = d
        ack = d.build_request("ACK", cseq=cseq)
        d._ack = ack.encode()
        self._sendto(d._ack, d._request_addr())
        return d

    async def _cancel(self, invite: SipMessage, addr: tuple[str, int]) -> None:
        cancel = SipMessage(f"CANCEL {invite.uri} SIP/2.0")
        for name in ("via", "from", "to", "call-id"):
            cancel.add(name, invite.get(name) or "")
        cancel.add("max-forwards", "70")
        cancel.add("cseq", f"{invite.cseq[0]} CANCEL")
        try:
            await self.transact(cancel, addr, timeout=4.0)
        except (SipError, asyncio.TimeoutError, OSError):
            pass

    # ------------------------------------------------------------ requests we receive
    def _reply(self, req: SipMessage, status: int, reason: str | None = None, *, to_tag: str | None = None,
               body: str = "", extra: list[tuple[str, str]] | None = None, addr: tuple[str, int] | None = None,
               local_ip: str | None = None) -> bytes:
        resp = SipMessage(f"SIP/2.0 {status} {reason or REASONS.get(status, '')}".strip())
        vias = req.get_list("via")
        for i, v in enumerate(vias):
            if i == 0 and addr:
                v = re.sub(r";rport(?==|;|$)(=\d*)?", "", v)
                v = f"{v};received={addr[0]};rport={addr[1]}"
            resp.add("via", v)
        resp.add("from", req.get("from") or "")
        to = req.get("to") or ""
        if to_tag and not header_tag(to):
            to += f";tag={to_tag}"
        resp.add("to", to)
        resp.add("call-id", req.call_id)
        resp.add("cseq", req.get("cseq") or "")
        for k, v in extra or []:
            resp.add(k, v)
        resp.add("user-agent", USER_AGENT)
        if body:
            resp.add("content-type", "application/sdp")
            resp.body = body.encode("utf-8")
        data = resp.encode()
        key = (req.call_id, req.cseq[0], req.method)
        if status >= 200:
            self._server_tx[key] = (data, addr or ("", 0))
            asyncio.get_running_loop().call_later(40.0, self._server_tx.pop, key, None)
        if addr:
            self._sendto(data, addr)
        return data

    def _on_request(self, req: SipMessage, addr: tuple[str, int]) -> None:
        method = req.method
        key = (req.call_id, req.cseq[0], method)
        if method != "ACK" and key in self._server_tx:  # a retransmission: say the same thing again
            self._sendto(self._server_tx[key][0], addr)
            return
        dialog = self._dialogs.get(req.call_id)
        if method == "OPTIONS":
            if self.on_options:
                self.on_options(addr[0])
            self._reply(req, 200, addr=addr, extra=[("allow", ALLOW), ("accept", "application/sdp")])
        elif method == "INVITE":
            if dialog is not None and header_tag(req.get("to")):
                self._on_reinvite(req, dialog, addr)
            elif req.call_id in self._incoming:
                return  # the first INVITE is still being decided; the 100 Trying is on its way
            else:
                self._spawn(self._handle_invite(req, addr))
        elif method == "ACK":
            if dialog is not None:
                dialog._acked.set()
        elif method == "BYE":
            if dialog is None:
                self._reply(req, 481, addr=addr)
            else:
                self._reply(req, 200, addr=addr)
                dialog.remote_hung_up()
        elif method == "CANCEL":
            pending = self._incoming.get(req.call_id)
            if pending is None:
                self._reply(req, 481, addr=addr)
            else:
                self._reply(req, 200, addr=addr)
                pending.cancelled.set()
        elif method == "INFO":
            self._reply(req, 200, addr=addr)
        else:
            self._reply(req, 501, addr=addr)

    def _on_reinvite(self, req: SipMessage, d: Dialog, addr: tuple[str, int]) -> None:
        offer = req.body.decode("utf-8", errors="replace")
        ok = True
        if offer.strip() and d.on_remote_sdp:
            ok = d.on_remote_sdp(offer)
        if not ok:
            self._reply(req, 488, addr=addr)
            return
        if offer.strip():
            d.remote_sdp = offer
        d.remote_cseq = req.cseq[0]
        self._reply(req, 200, addr=addr, to_tag=d.local_tag, body=d.local_sdp,
                    extra=[("contact", self.contact(d.local_ip))])

    async def _handle_invite(self, req: SipMessage, addr: tuple[str, int]) -> None:
        call_id = req.call_id
        offer = req.body.decode("utf-8", errors="replace")
        incoming = IncomingCall(call_id, req.get("from") or "", addr[0], offer)
        self._incoming[call_id] = incoming
        to_tag = new_tag()
        try:
            self._reply(req, 100, addr=addr)
            if self.on_invite is None:
                decision: Accept | Reject = Reject(603, "Decline")
            else:
                decide = asyncio.ensure_future(self.on_invite(incoming))
                gone = asyncio.ensure_future(incoming.cancelled.wait())
                done, _ = await asyncio.wait({decide, gone}, return_when=asyncio.FIRST_COMPLETED)
                gone.cancel()
                if decide not in done:
                    decide.cancel()
                    self._reply(req, 487, addr=addr, to_tag=to_tag)
                    return
                decision = decide.result()
            if isinstance(decision, Reject):
                self._reply(req, decision.status, decision.reason or None, addr=addr, to_tag=to_tag)
                return
            ip = self.local_ip_for(addr)
            contact = self.contact(ip)
            self._reply(req, 200, addr=addr, to_tag=to_tag, body=decision.sdp, extra=[
                ("contact", contact), ("allow", ALLOW)] + [("record-route", r) for r in req.get_list("record-route")])
            remote_contact = bare_uri((req.get_list("contact") or [""])[0], f"sip:{addr[0]}:{addr[1]}").split(";")[0]
            d = Dialog(self, call_id, bare_uri(req.get("to"), f"sip:{self.user}@{ip}").split(";")[0],
                       bare_uri(req.get("from"), remote_contact).split(";")[0], to_tag,
                       header_tag(req.get("from")) or "", remote_contact, req.get_list("record-route"), "uas", 0,
                       req.cseq[0], ip, addr)
            d.local_sdp = decision.sdp
            d.remote_sdp = offer
            self._dialogs[call_id] = d
            if self.on_accepted:
                self.on_accepted(d)
            self._spawn(self._await_ack(d, req, addr))
        except Exception:
            log.exception("answering a call from %s failed", addr)
            self._reply(req, 500, addr=addr, to_tag=to_tag)
        finally:
            self._incoming.pop(call_id, None)

    async def _await_ack(self, d: Dialog, invite: SipMessage, addr: tuple[str, int]) -> None:
        """The 200 OK is retransmitted until the ACK arrives (RFC 3261 13.3.1.4); without one the call is dropped."""
        key = (invite.call_id, invite.cseq[0], "INVITE")
        interval, waited = T1, 0.0
        while waited < 32.0:
            try:
                await asyncio.wait_for(d._acked.wait(), interval)
                return
            except asyncio.TimeoutError:
                waited += interval
                stored = self._server_tx.get(key)
                if stored:
                    self._sendto(stored[0], addr)
                interval = min(interval * 2, T2)
        log.warning("no ACK for the call from %s", addr)
        d.remote_hung_up("no ack")
