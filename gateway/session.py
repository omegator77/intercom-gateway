"""Keeps one line connected for as long as the gateway runs: login (cached) -> audio -> session ->
heartbeat loop, and rebuilds everything with backoff whenever anything breaks."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from enum import Enum
from pathlib import Path
from typing import Callable

import httpx

from .audio import LineAudio
from .auth import AuthClient, LoginError
from .config import AudioCfg, DebugCfg, LineCfg, ServerCfg
from .debugrec import Recorder
from .rtc import LineConnection, SessionError

log = logging.getLogger("gateway.session")

HEARTBEAT_S = 10.0  # same cadence as the browser frontend
MAX_HEARTBEAT_FAILURES = 3
PARTICIPANTS_S = 5.0  # how often the list of people on the line is refreshed (display only)
PARTICIPANTS_MAX_FAILURES = 3  # then the list is shown as unknown instead of stale
STABLE_S = 60.0  # connected this long -> next failure starts the backoff from scratch
BACKOFF_MAX_S = 30.0


class State(str, Enum):
    IDLE = "idle"
    LOGIN = "login"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    BACKOFF = "backoff"
    STOPPED = "stopped"


AudioFactory = Callable[[LineCfg, AudioCfg, asyncio.AbstractEventLoop], LineAudio]


class LineWorker:
    def __init__(self, cfg: LineCfg, server: ServerCfg, audio_cfg: AudioCfg, audio_factory: AudioFactory,
                 http: httpx.AsyncClient, debug: DebugCfg = DebugCfg(), auth: AuthClient | None = None):
        self.cfg = cfg
        self._debug = debug
        self._recorder: Recorder | None = None
        self._server = server
        self._audio_cfg = audio_cfg
        self._audio_factory = audio_factory
        self._http = http  # shared by all lines (see gateway/http.py)
        # Lines of one account (the intercom client) can share one login: it is rate-limited per address
        self.auth = auth or AuthClient(http, cfg.username, cfg.password)
        self._stop = asyncio.Event()
        self.state = State.IDLE
        self.last_error: str | None = None
        self.session_id: str | None = None
        self.sessions_started = 0
        self.connected_since: float | None = None
        self.audio: LineAudio | None = None
        self.conn: LineConnection | None = None
        self.participants: list[dict] | None = None  # others on the line; None = not known (not connected / fetch failing)
        self.program_line: bool | None = None  # the line is the production's programme line (from the same poll)

    def stop(self) -> None:
        self._stop.set()

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            started = time.monotonic()
            delay_floor = 0.0
            try:
                await self._once()
            except asyncio.CancelledError:
                raise
            except LoginError as e:
                self.last_error = str(e)
                log.error("[%s] %s", self.cfg.name, e)
                delay_floor = e.retry_after or (60.0 if e.status in (400, 401, 403) else 0.0)
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("[%s] session ended: %s", self.cfg.name, self.last_error)
            self.session_id = None
            self.connected_since = None
            self.participants = None
            self.program_line = None
            if self._stop.is_set():
                break
            backoff = 1.0 if time.monotonic() - started > STABLE_S else min(backoff * 2, BACKOFF_MAX_S)
            self.state = State.BACKOFF
            delay = max(delay_floor, backoff * (0.8 + 0.4 * random.random()))
            log.info("[%s] reconnecting in %.1f s", self.cfg.name, delay)
            await self._sleep(delay)
        self.state = State.STOPPED

    async def _once(self) -> None:
        self.state = State.LOGIN
        token = await self.auth.token()

        self.state = State.CONNECTING
        loop = asyncio.get_running_loop()
        audio = self._audio_factory(self.cfg, self._audio_cfg, loop)
        self.audio = audio
        if self._debug.record_dir:
            self._recorder = Recorder(Path(self._debug.record_dir), self.cfg.name, self._debug.record_seconds)
            audio.recorder = self._recorder
            log.warning("[%s] diagnostic recording is ON (%.0f s) -> %s", self.cfg.name, self._debug.record_seconds,
                        self._debug.record_dir)
        await audio.start()
        conn = LineConnection(self._http, token, audio.track, audio.on_remote_pcm, self._server.ice_servers,
                              relay_type=None if self.cfg.relay_type == "ssrc-rewrite" else self.cfg.relay_type,
                              recorder=self._recorder,
                              opus=(self.cfg.opus_mode, self.cfg.opus_bitrate_kbps),
                              stereo_rx=self.cfg.stereo_output, refresh_token=self._fresh_token)
        self.conn = conn
        try:
            try:
                self.session_id = await conn.connect(self.cfg.production_id, self.cfg.line_id, self.cfg.name)
            except SessionError as e:
                if e.status in (401, 403):
                    self.auth.invalidate()  # token revoked/expired: log in again on the next attempt
                raise
            self.sessions_started += 1
            self.state = State.CONNECTED
            self.connected_since = time.monotonic()
            self.last_error = None
            log.info("[%s] connected (session %s, %.0f ms)", self.cfg.name, self.session_id, conn.connected_ms or 0)
            await self._monitor(conn)
        finally:
            for what, coro in (("connection", conn.close()), ("audio", audio.stop())):
                try:
                    await asyncio.wait_for(coro, 10)
                except Exception as e:  # a stuck close must not keep the line from reconnecting or the process from exiting
                    log.warning("[%s] closing %s failed: %s", self.cfg.name, what, e)
            self.conn = None
            self.audio = None
            if self._recorder:
                self._recorder.finish()
                self._recorder = None

    async def _fresh_token(self, stale: str) -> str:
        """The server revoked the login this connection was made with (the account was logged out somewhere): log in
        again - once for all lines of the account - and use the new token from now on."""
        self.auth.invalidate_if(stale)
        token = await self.auth.token()
        log.info("[%s] logged in again (the server had revoked the login)", self.cfg.name)
        return token

    async def _monitor(self, conn: LineConnection) -> None:
        stop_t = asyncio.ensure_future(self._stop.wait())
        dead_t = asyncio.ensure_future(conn.dead.wait())
        failures = 0
        people_t = asyncio.ensure_future(self._poll_participants(conn))
        try:
            while True:
                done, _ = await asyncio.wait({stop_t, dead_t}, timeout=HEARTBEAT_S, return_when=asyncio.FIRST_COMPLETED)
                if self._recorder and self._recorder.full:
                    self._recorder.finish()
                if stop_t in done:
                    return
                if dead_t in done:
                    raise SessionError("peer connection lost")
                try:
                    status = await conn.heartbeat()
                except Exception as e:
                    failures += 1
                    log.warning("[%s] heartbeat failed (%d/%d): %s", self.cfg.name, failures, MAX_HEARTBEAT_FAILURES, e)
                else:
                    if status == 410:
                        raise SessionError("server no longer knows this session (410)")
                    if status == 200:
                        failures = 0
                    else:
                        failures += 1
                        log.warning("[%s] heartbeat HTTP %s (%d/%d)", self.cfg.name, status, failures, MAX_HEARTBEAT_FAILURES)
                if failures >= MAX_HEARTBEAT_FAILURES:
                    raise SessionError("heartbeat failed repeatedly")
        finally:
            stop_t.cancel()
            dead_t.cancel()
            people_t.cancel()
            self.participants = None

    async def _poll_participants(self, conn: LineConnection) -> None:
        """Display only: who else is on the line. Never raises and never touches the session or the audio -
        a failing or slow fetch just leaves the list as 'unknown'."""
        failures = 0
        while not self._stop.is_set():
            try:
                self.participants = await asyncio.wait_for(
                    conn.participants(self.cfg.production_id, self.cfg.line_id), PARTICIPANTS_S)
                self.program_line = getattr(conn, "program_line", None)
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failures += 1
                log.debug("[%s] fetching the participants failed (%d): %s", self.cfg.name, failures, e)
                if failures >= PARTICIPANTS_MAX_FAILURES:
                    self.participants = None
            await self._sleep(PARTICIPANTS_S)

    def status(self) -> dict:
        in_db, out_db = self.audio.levels() if self.audio else (-120.0, -120.0)  # same floor as audio.Level
        return {
            "line": self.cfg.name,
            "production_id": self.cfg.production_id,
            "line_id": self.cfg.line_id,
            "username": self.cfg.username,
            "input_device": self.cfg.input_device,
            "output_device": self.cfg.output_device,
            "state": self.state.value,
            "session": self.session_id,
            "sessions_started": self.sessions_started,
            "uptime_s": round(time.monotonic() - self.connected_since) if self.connected_since else 0,
            "in_dbfs": round(in_db, 1),
            "out_dbfs": round(out_db, 1),
            "audio": self.audio.stats() if self.audio else {},
            "rx_lost": self.conn.rx_lost if self.conn else 0,
            "last_error": self.last_error,
            "participants": self.participants,
            "program_line": self.program_line,
        }
