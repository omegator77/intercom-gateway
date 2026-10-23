"""One configured call: dials (or waits for) the other side, joins the lines while the call is up, bridges the audio
and starts over when the call ends or dies. `SipService` runs all calls of a configuration on one SIP endpoint."""
from __future__ import annotations

import asyncio
import logging
import random
import socket
import time
from enum import Enum

import httpx

from gateway.auth import AuthClient
from gateway.config import AudioCfg, DebugCfg, LineCfg, ServerCfg
from gateway.session import LineWorker

from .codecs import AudioFormat
from .config import CallCfg, SipCfg, SipConfig
from .lines import LineBinding, SipLineAudio
from .media import CallMedia
from .registration import Registration
from .sdp import SdpError, build_sdp, choose_codec, describe_sdp, parse_sdp, payload_type
from .sip import Accept, Dialog, IncomingCall, Reject, SipEndpoint, SipError, parse_uri

log = logging.getLogger("sip.call")

LINE_STOP_WAIT_S = 12.0


class CallState(str, Enum):
    IDLE = "idle"
    DIALING = "dialing"
    RINGING = "ringing"
    CONNECTED = "connected"
    WAITING = "waiting"  # answering call: ready for the other side to call
    BACKOFF = "backoff"
    STOPPED = "stopped"


class CallRunner:
    def __init__(self, cfg: CallCfg, ua: SipEndpoint, sip: SipCfg, server: ServerCfg, audio: AudioCfg,
                 lines: dict[str, LineCfg], http: httpx.AsyncClient, auths: dict[str, AuthClient],
                 debug: DebugCfg = DebugCfg()):
        self.cfg = cfg
        self._ua, self._sip, self._server, self._audio, self._http, self._debug = ua, sip, server, audio, http, debug
        self._line_cfgs = {n: lines[n] for n in cfg.line_names}
        self._auths = auths
        # one persistent binding per line: listened to when some channel sends it, spoken into when some channel receives
        sends = {c.send for c in cfg.channels if c.send}
        receives = {c.receive for c in cfg.channels if c.receive}
        self.bindings = {n: LineBinding(n, n in sends, n in receives, cfg.in_gain_db, cfg.out_gain_db)
                         for n in self._line_cfgs}
        self.registration_domain: str | None = None  # set by the service when a registration is configured
        self.workers: dict[str, LineWorker] = {}
        self._worker_tasks: dict[str, asyncio.Task] = {}
        self.state = CallState.IDLE
        self.last_error: str | None = None
        self.calls_made = 0
        self.connected_since: float | None = None
        self.dialog: Dialog | None = None
        self.media: CallMedia | None = None
        self.remote_agent = ""
        self._stop = asyncio.Event()
        self._busy = False
        self._answer: asyncio.Future | None = None  # answering: resolves to (dialog, media, fmt) when a call is established
        self._prepared: dict[str, tuple[CallMedia, AudioFormat]] = {}  # call id -> media waiting for its dialog

    # ------------------------------------------------------------ control
    def stop(self) -> None:
        self._stop.set()
        if self._answer and not self._answer.done():
            self._answer.cancel()

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    def _channel_lines(self, which: str) -> list[LineBinding | None]:
        names = [getattr(c, which) for c in self.cfg.channels]
        return [self.bindings[n] if n else None for n in names]

    def _new_media(self, fmt: AudioFormat, pt: int | None = None) -> CallMedia:
        return CallMedia(fmt, pt if pt is not None else payload_type(fmt), self._channel_lines("send"),
                         self._channel_lines("receive"), self.cfg.playout_ms, self.cfg.rtp_latch)

    # ------------------------------------------------------------ the main loop
    async def run(self) -> None:
        if self.cfg.keep_lines:
            await self._start_lines()
        try:
            if self.cfg.mode == "dial":
                await self._run_dialer()
            else:
                await self._run_answerer()
        finally:
            await self._stop_lines()
            self.state = CallState.STOPPED

    async def _run_dialer(self) -> None:
        backoff = 0.0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await self._dial_once()
            except asyncio.CancelledError:
                raise
            except SipError as e:
                self.last_error = f"{e.status} {e.reason}".strip()
                log.warning("[%s] call failed: %s", self.cfg.name, self.last_error)
            except (SdpError, OSError) as e:
                self.last_error = str(e)
                log.warning("[%s] call failed: %s", self.cfg.name, e)
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("[%s] call failed unexpectedly", self.cfg.name)
            self.connected_since = None
            if self._stop.is_set() or not self.cfg.redial:
                break
            # a call that lasted a while ended for a reason that is probably gone: call again at once
            backoff = 0.0 if time.monotonic() - started > 60 else self.cfg.redial_s
            self.state = CallState.BACKOFF
            delay = backoff * (0.8 + 0.4 * random.random())
            log.info("[%s] calling again in %.0f s", self.cfg.name, delay)
            await self._sleep(delay)

    async def _dial_once(self) -> None:
        cfg = self.cfg
        target = cfg.target
        assert target is not None
        uri = parse_uri(target)
        reg = self._sip.register
        # a target in the registrar's domain is reached through the registrar; anything else (an IP) directly
        proxy = reg.registrar if reg is not None and uri.host in (reg.domain, parse_uri(reg.registrar).host) else None
        hop = parse_uri(proxy) if proxy else uri
        addr = await self._ua.resolve(hop.host, hop.port)
        ip = self._sip.local_ip or self._ua.local_ip_for(addr)
        media = self._new_media(cfg.fmt)
        port = await media.open(self._sip.bind_ip, self._sip.rtp_port_min, self._sip.rtp_port_max)
        offer = build_sdp(ip, port, cfg.fmt)
        self.state = CallState.DIALING
        log.info("[%s] calling %s with %s (RTP port %d)", cfg.name, target, cfg.fmt.describe(), port)

        def progress(status: int, reason: str) -> None:
            if status in (180, 183):
                self.state = CallState.RINGING

        auth = (cfg.auth_user, cfg.auth_password) if cfg.auth_user else None
        domain = None
        if reg is not None:  # we are logged in at a registrar: our identity is user@domain
            domain = self.registration_domain
            if proxy and auth is None:  # ... and the same login is good for calls through it
                auth = (reg.auth_user or reg.user, reg.password)
        invite = asyncio.ensure_future(self._ua.invite(target, offer, auth=auth, progress=progress, domain=domain,
                                                       proxy=proxy))
        stopped = asyncio.ensure_future(self._stop.wait())
        try:
            await asyncio.wait({invite, stopped}, return_when=asyncio.FIRST_COMPLETED)
            if not invite.done():
                invite.cancel()
                await asyncio.gather(invite, return_exceptions=True)
                await media.close()
                return
            dialog = invite.result()
            log.info("[%s] the other side answered: %s", cfg.name, describe_sdp(dialog.remote_sdp))
            try:
                remote = parse_sdp(dialog.remote_sdp)
                chosen = next((c for c in remote.codecs if c.pt == payload_type(cfg.fmt)), None)
                if chosen is None or remote.on_hold:
                    raise SdpError("the other side did not accept our codec: "
                                   + (", ".join(c.name for c in remote.codecs) or "no codec in its answer"))
            except SdpError:
                await dialog.bye()
                raise
        except BaseException:
            await media.close()
            raise
        finally:
            stopped.cancel()
        await self._session(dialog, media, remote.address, remote.port)

    async def _run_answerer(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            self.state = CallState.WAITING
            self._answer = loop.create_future()
            try:
                dialog, media, remote = await self._answer
            except asyncio.CancelledError:
                if self._stop.is_set():
                    break
                continue
            finally:
                self._answer = None
            try:
                await self._session(dialog, media, remote.address, remote.port)
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("[%s] call failed", self.cfg.name)
            self.connected_since = None

    # ------------------------------------------------------------ incoming calls (answer mode)
    async def handle_incoming(self, call: IncomingCall) -> "Accept | Reject":
        if self._busy or self.state != CallState.WAITING:
            log.info("[%s] refusing a call from %s: busy", self.cfg.name, call.remote_ip)
            return Reject(486, "Busy Here")
        log.info("[%s] incoming call from %s offers: %s", self.cfg.name, call.remote_ip, describe_sdp(call.sdp))
        try:
            remote = parse_sdp(call.sdp)
            rc, fmt = choose_codec(remote, self.cfg.fmt)
        except SdpError as e:
            log.warning("[%s] refusing a call from %s: %s", self.cfg.name, call.remote_ip, e)
            self.last_error = str(e)
            return Reject(488, "Not Acceptable Here")
        media = self._new_media(fmt, rc.pt)  # the other side chose the payload type number; we answer with the same one
        try:
            port = await media.open(self._sip.bind_ip, self._sip.rtp_port_min, self._sip.rtp_port_max)
        except OSError as e:
            self.last_error = str(e)
            return Reject(503, "Service Unavailable")
        self._busy = True
        self._prepared[call.call_id] = (media, fmt)
        ip = self._sip.local_ip or self._ua.local_ip_for((call.remote_ip, 5060))
        log.info("[%s] answering a call from %s with %s", self.cfg.name, call.remote_ip, fmt.describe())
        asyncio.get_running_loop().call_later(40.0, self._expire_prepared, call.call_id)
        return Accept(build_sdp(ip, port, fmt, pt=rc.pt))

    def _expire_prepared(self, call_id: str) -> None:
        """The call we accepted never became a dialog (the caller vanished before it was established)."""
        entry = self._prepared.pop(call_id, None)
        if entry is not None:
            asyncio.ensure_future(self._drop_media(entry[0]))

    async def _drop_media(self, media: CallMedia) -> None:
        await media.close()
        self._busy = False

    def dialog_ready(self, dialog: Dialog) -> None:
        """An incoming call we accepted is now established."""
        media, fmt = self._prepared.pop(dialog.call_id, (None, None))
        if media is None:
            return
        remote = parse_sdp(dialog.remote_sdp)
        if self._answer and not self._answer.done():
            self._answer.set_result((dialog, media, remote))
        else:  # we were told to stop while the call was coming up
            asyncio.ensure_future(self._drop(dialog, media))

    async def _drop(self, dialog: Dialog, media: CallMedia) -> None:
        await dialog.bye()
        await media.close()
        self._busy = False

    # ------------------------------------------------------------ a call that is up
    async def _session(self, dialog: Dialog, media: CallMedia, address: str, port: int) -> None:
        self.dialog, self.media = dialog, media
        self.state = CallState.CONNECTED
        self.connected_since = time.monotonic()
        self.calls_made += 1
        self.last_error = None
        self._busy = True
        log.info("[%s] call is up: %s, RTP to %s:%d", self.cfg.name, media.fmt.describe(), address, port)

        def reinvite(sdp: str) -> bool:
            try:
                r = parse_sdp(sdp)
            except SdpError:
                return False
            if not any(c.pt == media.pt for c in r.codecs):
                return False
            media.retarget((r.address, r.port))
            return True

        dialog.on_remote_sdp = reinvite
        media.start((address, port))
        try:
            await self._start_lines()
            closed = asyncio.ensure_future(dialog.closed.wait())
            stopped = asyncio.ensure_future(self._stop.wait())
            try:
                while not (closed.done() or stopped.done()):
                    await asyncio.wait({closed, stopped}, timeout=1.0, return_when=asyncio.FIRST_COMPLETED)
                    if self.cfg.rtp_timeout_s and media.silent_for() > self.cfg.rtp_timeout_s:
                        self.last_error = f"no audio from the other side for {self.cfg.rtp_timeout_s:.0f} s"
                        log.warning("[%s] %s - hanging up", self.cfg.name, self.last_error)
                        break
            finally:
                closed.cancel()
                stopped.cancel()
            if dialog.closed.is_set():
                log.info("[%s] call ended (%s hung up)", self.cfg.name, dialog.closed_by)
                if dialog.closed_by == "remote":
                    self.last_error = "the other side hung up"
        finally:
            await dialog.bye()
            await media.close()
            if not self.cfg.keep_lines:
                await self._stop_lines()
            self.dialog = None
            self._busy = False
            log.info("[%s] call closed", self.cfg.name)

    # ------------------------------------------------------------ the lines
    async def _start_lines(self) -> None:
        for name, lcfg in self._line_cfgs.items():
            if name in self._worker_tasks and not self._worker_tasks[name].done():
                continue
            binding = self.bindings[name]
            worker = LineWorker(
                lcfg, self._server, self._audio,
                lambda cfg, audio, loop, _b=binding: SipLineAudio(_b, cfg, audio, loop),
                self._http, self._debug, auth=self._auths[name])
            self.workers[name] = worker
            self._worker_tasks[name] = asyncio.ensure_future(worker.run())

    async def _stop_lines(self) -> None:
        tasks = list(self._worker_tasks.values())
        for w in self.workers.values():
            w.stop()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=LINE_STOP_WAIT_S)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        self._worker_tasks.clear()
        self.workers.clear()

    # ------------------------------------------------------------ status
    def status(self) -> dict:
        return {
            "call": self.cfg.name,
            "mode": self.cfg.mode,
            "target": self.cfg.target,
            "state": self.state.value,
            "calls": self.calls_made,
            "uptime_s": round(time.monotonic() - self.connected_since) if self.connected_since else 0,
            "last_error": self.last_error,
            "media": self.media.stats() if self.media and self.state == CallState.CONNECTED else None,
            "lines": {n: {"state": w.state.value, "last_error": w.last_error,
                          "listens": self.bindings[n].listens, "talks": self.bindings[n].talks,
                          "participants": w.participants} for n, w in self.workers.items()},
        }


class SipService:
    """All calls of one configuration, sharing one SIP port, one HTTP client and one login per line account."""

    def __init__(self, cfg: SipConfig, http: httpx.AsyncClient):
        self.cfg = cfg
        self.http = http
        self.ua = SipEndpoint(cfg.sip.sip_port, cfg.sip.user, cfg.sip.display_name, cfg.sip.local_ip, cfg.sip.bind_ip)
        auths = {n: AuthClient(http, l.username, l.password) for n, l in cfg.lines.items()}
        self.runners = [CallRunner(c, self.ua, cfg.sip, cfg.server, cfg.audio, cfg.lines, http, auths, cfg.debug)
                        for c in cfg.calls]
        self.registration: Registration | None = None
        if cfg.sip.register:
            self.registration = Registration(self.ua, cfg.sip.register)
            for r in self.runners:
                r.registration_domain = self.registration.domain
        self._by_call_id: dict[str, CallRunner] = {}
        self.ua.on_invite = self._on_invite
        self.ua.on_accepted = self._on_accepted

    async def _on_invite(self, call: IncomingCall) -> "Accept | Reject":
        for r in self.runners:
            if r.cfg.mode == "answer" and await self._may_call(r.cfg, call):
                result = await r.handle_incoming(call)
                if isinstance(result, Accept):
                    self._by_call_id[call.call_id] = r
                return result
        log.warning("refusing a call from %s: not in any accept_from list", call.remote_ip)
        return Reject(403, "Forbidden")

    @staticmethod
    async def _may_call(cfg: CallCfg, call: IncomingCall) -> bool:
        if cfg.extension and call.to_user != cfg.extension:
            return False
        for entry in cfg.accept_from:
            if entry == call.remote_ip:
                return True
            if not entry.replace(".", "").isdigit():  # a host name: whoever it resolves to right now
                try:
                    infos = await asyncio.get_running_loop().getaddrinfo(entry, None, family=socket.AF_INET)
                except OSError:
                    continue
                if any(i[4][0] == call.remote_ip for i in infos):
                    return True
        return False

    def _on_accepted(self, dialog: Dialog) -> None:
        r = self._by_call_id.pop(dialog.call_id, None)
        if r is not None:
            r.dialog_ready(dialog)

    async def start(self) -> None:
        await self.ua.start()

    async def run(self) -> None:
        tasks = [r.run() for r in self.runners]
        reg = asyncio.ensure_future(self.registration.run()) if self.registration else None
        try:
            await asyncio.gather(*tasks)
        finally:
            if reg is not None:
                self.registration.stop()
                await asyncio.wait({reg}, timeout=8)
                reg.cancel()

    def stop(self) -> None:
        for r in self.runners:
            r.stop()

    async def close(self) -> None:
        await self.ua.close()

    def status(self) -> dict:
        reg = self.registration
        return {"sip_port": self.ua.port, "calls": [r.status() for r in self.runners],
                "registration": {"address": reg.address, "state": reg.state, "last_error": reg.last_error} if reg else None}
