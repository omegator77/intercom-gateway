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
from .registration import RegisterCfg
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
                 debug: DebugCfg = DebugCfg(), account: RegisterCfg | None = None):
        self.cfg = cfg
        self.account = account
        self._ua, self._sip, self._server, self._audio, self._http, self._debug = ua, sip, server, audio, http, debug
        self._line_cfgs = {n: lines[n] for n in cfg.line_names}
        self._auths = auths
        # one persistent binding per line: listened to when some channel sends it, spoken into when some channel receives
        sends = {c.send for c in cfg.channels if c.send}
        receives = {c.receive for c in cfg.channels if c.receive}
        self.bindings = {n: LineBinding(n, n in sends, n in receives, cfg.in_gain_db, cfg.out_gain_db)
                         for n in self._line_cfgs}
        self.registration_domain: str | None = None  # "@domain" of the account's address (set by the service)
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
        self._wake = asyncio.Event()  # a request from the status page (call now / stop calling)
        self._hangup = asyncio.Event()
        self._dial_target: str | None = None
        self._one_off = False  # the call asked for was a number typed in by hand
        self._paused = not cfg.autostart  # a dialling profile does not call by itself while paused
        self.uid = 0  # stable number of this profile while the service runs (the page addresses profiles by it)
        self.direction = ""  # "out" or "in" for the call that is up
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
            await self._main()
        finally:
            await self._stop_lines()
            self.state = CallState.STOPPED

    @property
    def can_answer(self) -> bool:
        return self.cfg.mode in ("answer", "both")

    @property
    def can_dial(self) -> bool:
        return self.cfg.mode in ("dial", "both")

    @property
    def in_call(self) -> bool:
        return self.state in (CallState.DIALING, CallState.RINGING, CallState.CONNECTED)

    async def _main(self) -> None:
        loop = asyncio.get_running_loop()
        retry_at = 0.0  # calling again by itself: not before this time (monotonic)
        while not self._stop.is_set():
            target: str | None = None
            if self._dial_target:
                target, self._dial_target = self._dial_target, None
            elif self.cfg.mode == "dial" and not self._paused and time.monotonic() >= retry_at:
                target = self.cfg.target
            if target:
                self._hangup.clear()
                started = time.monotonic()
                one_off, self._one_off = self._one_off, False
                await self._call_out(target)
                self.connected_since = None
                if self._stop.is_set():
                    break
                if self.cfg.mode == "dial":
                    if one_off:
                        self._paused = True  # a number typed in by hand is called once; the automatic call stays off
                    elif not self._paused:
                        if self.cfg.redial:
                            # a call that lasted a while ended for a reason that is probably gone: call again at once
                            delay = 0.0 if time.monotonic() - started > 60 else self.cfg.redial_s * (0.8 + 0.4 * random.random())
                            retry_at = time.monotonic() + delay
                            log.info("[%s] calling again in %.0f s", self.cfg.name, delay)
                        else:
                            self._paused = True
                continue
            # nothing to do right now: wait for a call, for a request from the page, or for the time to call again
            auto_pending = self.cfg.mode == "dial" and not self._paused
            self.state = (CallState.WAITING if self.can_answer else CallState.BACKOFF if auto_pending
                          else CallState.IDLE)
            self._wake.clear()
            waits = {asyncio.ensure_future(self._stop.wait()), asyncio.ensure_future(self._wake.wait())}
            answer: asyncio.Future | None = None
            if self.can_answer:
                answer = self._answer = loop.create_future()
                waits.add(answer)
            timeout = max(0.0, retry_at - time.monotonic()) if auto_pending else None
            try:
                await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            finally:
                self._answer = None
                for w in waits:
                    if not w.done():
                        w.cancel()  # an answer future that was cancelled makes a late `dialog_ready` drop its call
            if answer is not None and answer.done() and not answer.cancelled():
                dialog, media, remote = answer.result()
                try:
                    await self._session(dialog, media, remote.address, remote.port, "in")
                except Exception as e:
                    self.last_error = f"{type(e).__name__}: {e}"
                    log.exception("[%s] call failed", self.cfg.name)
                self.connected_since = None

    async def _call_out(self, target: str) -> None:
        try:
            await self._dial_once(target)
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

    # ------------------------------------------------------------ requests from the status page
    def _expand_target(self, raw: str) -> str:
        """A bare number is dialled in the account's domain; raises SipError for something that is no address."""
        acc = self.account
        if acc is not None and "@" not in raw and not raw.startswith("sip:"):
            raw = f"sip:{raw}@{acc.domain or parse_uri(acc.registrar).host}"
        parse_uri(raw)
        return raw

    def request_dial(self, number: str | None = None) -> str | None:
        """Call now (the number typed in, or the profile's own target). None = started, otherwise the reason why not."""
        if not self.can_dial:
            return "dieses Profil nimmt nur Anrufe an"
        if self._stop.is_set() or self.state == CallState.STOPPED:
            return "der Dienst wird beendet"
        if self.in_call or self._busy:
            return "es läuft schon ein Anruf"
        raw = (number or "").strip() or (self.cfg.target or "")
        if not raw:
            return "keine Rufnummer angegeben"
        try:
            target = self._expand_target(raw)
        except SipError as e:
            return e.reason or "ungültige Rufnummer"
        self._one_off = bool((number or "").strip()) and target != self.cfg.target
        if not self._one_off:
            self._paused = False  # the automatic call of a dialling profile is back on
        self._dial_target = target
        self._wake.set()
        return None

    def request_hangup(self) -> str | None:
        """Hang up (or, while waiting to call again, stop calling). None = done, otherwise the reason why not."""
        if self.in_call:
            self._hangup.set()
            if self.cfg.mode == "dial":
                self._paused = True  # no automatic calling again until somebody presses "call"
            return None
        if self.cfg.mode == "dial" and not self._paused:
            self._paused = True
            self._wake.set()
            return None
        return "es läuft kein Anruf"

    async def _dial_once(self, target: str) -> None:
        cfg = self.cfg
        uri = parse_uri(target)
        acc = self.account
        proxy = acc.registrar if acc is not None else None  # a call of an account goes through its registrar
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
        caller = domain = contact_user = None
        if acc is not None:  # our identity is user@domain of the account, and its login is good for the call
            caller, domain, contact_user = acc.user, self.registration_domain, acc.contact_user or acc.user
            if auth is None:
                auth = (acc.auth_user or acc.user, acc.password)
        invite = asyncio.ensure_future(self._ua.invite(target, offer, auth=auth, progress=progress, caller=caller,
                                                       domain=domain, proxy=proxy, contact_user=contact_user))
        stopped = asyncio.ensure_future(self._stop.wait())
        hung_up = asyncio.ensure_future(self._hangup.wait())
        try:
            await asyncio.wait({invite, stopped, hung_up}, return_when=asyncio.FIRST_COMPLETED)
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
            hung_up.cancel()
        await self._session(dialog, media, remote.address, remote.port, "out")

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
    async def _session(self, dialog: Dialog, media: CallMedia, address: str, port: int, direction: str) -> None:
        self.dialog, self.media, self.direction = dialog, media, direction
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
            hung_up = asyncio.ensure_future(self._hangup.wait())
            try:
                while not (closed.done() or stopped.done() or hung_up.done()):
                    await asyncio.wait({closed, stopped, hung_up}, timeout=1.0, return_when=asyncio.FIRST_COMPLETED)
                    if self.cfg.rtp_timeout_s and media.silent_for() > self.cfg.rtp_timeout_s:
                        self.last_error = f"no audio from the other side for {self.cfg.rtp_timeout_s:.0f} s"
                        log.warning("[%s] %s - hanging up", self.cfg.name, self.last_error)
                        break
            finally:
                closed.cancel()
                stopped.cancel()
                hung_up.cancel()
            if hung_up.done() and not dialog.closed.is_set():
                log.info("[%s] hung up from the status page", self.cfg.name)
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
            self._hangup.clear()
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
        d = self.dialog
        return {
            "call": self.cfg.name,
            "mode": self.cfg.mode,
            "account": self.cfg.account,
            "target": self.cfg.target,
            "wanted": self.cfg.fmt.describe(),
            "channels": [{"send": c.send, "receive": c.receive} for c in self.cfg.channels],
            "state": self.state.value,
            "paused": self._paused and self.cfg.mode == "dial",
            "can_dial": self.can_dial and not self.in_call and not self._busy and self.state != CallState.STOPPED,
            "can_hangup": self.in_call or (self.cfg.mode == "dial" and not self._paused),
            "direction": self.direction if self.state == CallState.CONNECTED else "",
            "peer": d.remote_uri if d is not None and self.state == CallState.CONNECTED else None,
            "calls": self.calls_made,
            "uptime_s": round(time.monotonic() - self.connected_since) if self.connected_since else 0,
            "last_error": self.last_error,
            "media": self.media.stats() if self.media and self.state == CallState.CONNECTED else None,
            "lines": {n: {"state": w.state.value, "last_error": w.last_error,
                          "listens": self.bindings[n].listens, "talks": self.bindings[n].talks,
                          "participants": w.participants} for n, w in self.workers.items()},
        }


class ReconfigError(Exception):
    """A change of the configuration cannot be applied right now; the message is fit to show the user."""


class SipService:
    """All calls of one configuration, sharing one SIP port, one HTTP client and one login per line account.
    Accounts and call profiles can be added, changed and removed while it runs (`apply`)."""

    def __init__(self, cfg: SipConfig, http: httpx.AsyncClient):
        self.cfg = cfg
        self.http = http
        self.ua = SipEndpoint(cfg.sip.sip_port, cfg.sip.user, cfg.sip.display_name, cfg.sip.local_ip, cfg.sip.bind_ip)
        self._auths: dict[str, AuthClient] = {}
        self.registrations: dict[str, Registration] = {}
        self.runners: list[CallRunner] = []
        self._next_uid = 0
        self._runner_tasks: dict[int, asyncio.Task] = {}
        self._reg_tasks: dict[str, asyncio.Task] = {}
        self._closing = asyncio.Event()
        self._running = False
        self._sync_auths(cfg)
        for a in cfg.accounts.values():
            self._add_registration(a)
        for c in cfg.calls:
            self._add_runner(c)
        self._by_call_id: dict[str, CallRunner] = {}
        self.ua.on_invite = self._on_invite
        self.ua.on_accepted = self._on_accepted

    # ------------------------------------------------------------ the parts
    def _sync_auths(self, cfg: SipConfig) -> None:
        """One login per line account, kept as long as the account's name and password stay the same."""
        for n, l in cfg.lines.items():
            old = self._auths.get(n)
            if old is None or (old._user, old._pw) != (l.username, l.password):
                self._auths[n] = AuthClient(self.http, l.username, l.password)
        for n in [n for n in self._auths if n not in cfg.lines]:
            del self._auths[n]

    def _add_registration(self, acc: RegisterCfg) -> None:
        reg = self.registrations[acc.name] = Registration(self.ua, acc)
        if self._running:
            self._reg_tasks[acc.name] = asyncio.ensure_future(reg.run())

    async def _remove_registration(self, name: str) -> None:
        reg = self.registrations.pop(name)
        reg.stop()
        task = self._reg_tasks.pop(name, None)
        if task is not None:
            await asyncio.wait({task}, timeout=8)
            task.cancel()

    def _add_runner(self, c: CallCfg) -> CallRunner:
        r = CallRunner(c, self.ua, self.cfg.sip, self.cfg.server, self.cfg.audio, self.cfg.lines, self.http, self._auths,
                       self.cfg.debug, self.cfg.accounts.get(c.account) if c.account else None)
        r.uid = self._next_uid
        self._next_uid += 1
        if c.account:
            r.registration_domain = self.registrations[c.account].domain
        self.runners.append(r)
        if self._running:
            self._runner_tasks[r.uid] = asyncio.ensure_future(r.run())
        return r

    async def _remove_runner(self, r: CallRunner) -> None:
        r.stop()
        task = self._runner_tasks.pop(r.uid, None)
        if task is not None:
            done, pending = await asyncio.wait({task}, timeout=LINE_STOP_WAIT_S + 5)
            for t in pending:
                t.cancel()
        self.runners.remove(r)

    @staticmethod
    def _fingerprint(cfg: SipConfig, c: CallCfg):
        """Everything a running call profile depends on: when it differs, the profile is replaced."""
        return (c, cfg.accounts.get(c.account) if c.account else None, tuple(cfg.lines[n] for n in c.line_names))

    def runner(self, uid: int) -> CallRunner | None:
        return next((r for r in self.runners if r.uid == uid), None)

    async def apply(self, new: SipConfig) -> None:
        """Make the running service match `new`: accounts and call profiles are added, replaced or removed, the rest
        keeps running untouched. Raises ReconfigError, with nothing changed, if a profile that has to be replaced
        is in a call right now."""
        wanted = {c.name: c for c in new.calls}
        change = [r for r in self.runners
                  if r.cfg.name not in wanted or self._fingerprint(self.cfg, r.cfg) != self._fingerprint(new, wanted[r.cfg.name])]
        busy = [r.cfg.name for r in change if r.in_call or r._busy]
        if busy:
            raise ReconfigError("Gerade im Gespräch: " + ", ".join(f"„{n}“" for n in busy) + ". Erst auflegen, dann ändern.")
        for r in change:
            await self._remove_runner(r)
        for name in list(self.registrations):
            if name not in new.accounts or new.accounts[name] != self.registrations[name].cfg:
                await self._remove_registration(name)
        self.cfg = new
        self._sync_auths(new)
        for name, acc in new.accounts.items():
            if name not in self.registrations:
                self._add_registration(acc)
        have = {r.cfg.name for r in self.runners}
        for c in new.calls:
            if c.name not in have:
                self._add_runner(c)

    async def _on_invite(self, call: IncomingCall) -> "Accept | Reject":
        for r in self.runners:
            if r.can_answer and await self._may_call(r.cfg, call):
                result = await r.handle_incoming(call)
                if isinstance(result, Accept):
                    self._by_call_id[call.call_id] = r
                return result
        log.warning("refusing a call from %s: not in any accept_from list", call.remote_ip)
        return Reject(403, "Forbidden")

    async def _may_call(self, cfg: CallCfg, call: IncomingCall) -> bool:
        acc = cfg.account and self.cfg.accounts[cfg.account]
        if acc:  # the call must be for this account: its contact name, or the number it is registered as
            if call.ruri_user != (acc.contact_user or acc.user) and call.to_user != acc.user:
                return False
        entries = list(cfg.accept_from) + ([parse_uri(acc.registrar).host] if acc else [])
        for entry in entries:
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
        self._running = True
        for r in self.runners:
            self._runner_tasks.setdefault(r.uid, asyncio.ensure_future(r.run()))
        for n, reg in self.registrations.items():
            self._reg_tasks.setdefault(n, asyncio.ensure_future(reg.run()))
        await self._closing.wait()
        if self._runner_tasks:
            await asyncio.gather(*self._runner_tasks.values(), return_exceptions=True)
        for reg in self.registrations.values():
            reg.stop()
        if self._reg_tasks:
            await asyncio.wait(self._reg_tasks.values(), timeout=8)
            for t in self._reg_tasks.values():
                t.cancel()
        self._running = False

    def stop(self) -> None:
        self._closing.set()
        for r in self.runners:
            r.stop()

    async def close(self) -> None:
        await self.ua.close()

    def dial(self, uid: int, number: str | None = None) -> str | None:
        r = self.runner(uid)
        return r.request_dial(number) if r else "Dieses Anruf-Profil gibt es nicht."

    def hangup(self, uid: int) -> str | None:
        r = self.runner(uid)
        return r.request_hangup() if r else "Dieses Anruf-Profil gibt es nicht."

    def status(self) -> dict:
        return {"sip_port": self.ua.port, "calls": [dict(r.status(), id=r.uid) for r in self.runners],
                "registrations": [{"account": n, "address": r.address, "state": r.state, "last_error": r.last_error}
                                  for n, r in self.registrations.items()]}
