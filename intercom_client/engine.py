"""The client's engine: one login, one audio hub and one reconnecting LineWorker per line, run on an asyncio
loop in a background thread so a UI (or hotkeys, or Companion) can call the control methods from any thread.

Everything a person does is a method here: talk / stop talking on a line, latch, volume, mute what you hear,
mute all. The UI only calls these and reads `snapshot()`."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Callable

from gateway.audio import DeviceError
from gateway.auth import AuthClient
from gateway.config import AudioCfg, LineCfg, ServerCfg
from gateway.http import create_client
from gateway.session import LineWorker

from .companion import CompanionLink
from .config import ClientCfg, ClientLine
from .hotkeys import HotkeyError, HotkeyListener
from .hub import AudioHub, LineControl, SharedLineAudio, make_hub

log = logging.getLogger("client.engine")

OPUS_KBPS = 64  # a voice line; the gateway's 96 is for programme sound


def worker_line_cfg(cfg: ClientCfg, line: ClientLine) -> LineCfg:
    return LineCfg(name=line.name, production_id=line.production_id, line_id=line.line_id, username=cfg.username,
                   password=cfg.password, opus_bitrate_kbps=OPUS_KBPS,
                   playout_ms=cfg.audio.playout_ms)


class ClientEngine:
    def __init__(self, cfg: ClientCfg, *, enable_hotkeys: bool = True,
                 hub_factory: Callable[[object], AudioHub] = make_hub):
        self.cfg = cfg
        self.controls: dict[str, LineControl] = {l.name: LineControl(l.name, l.volume_pct) for l in cfg.lines}
        self.order: list[str] = [l.name for l in cfg.lines]  # the position of a line is its Companion button number
        self.hub: AudioHub | None = None
        self.workers: dict[str, LineWorker] = {}
        self.error: str | None = None  # why the engine could not start (devices, ...)
        self.hotkey_status = "off"  # "ok", "off", or the reason global hotkeys are unavailable
        self._global_mute = False  # kept here too, because the hub only exists once the audio is open
        self.line_enabled: dict[str, bool] = {l.name: True for l in cfg.lines}  # connected (or connecting) by choice
        self._tasks: dict[str, asyncio.Task] = {}
        self._closing = False  # shutting down: no new line worker may be started any more
        a = cfg.audio
        # microphone settings as they are right now (the window can change them while the lines keep running)
        self.mic_gain_db = a.mic_gain_db
        self.echo, self.noise, self.agc = a.echo_cancellation, a.noise_suppression, a.auto_gain
        self._companion: CompanionLink | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._factory = None
        self._http = None
        self._auth = None
        self._server = ServerCfg(cfg.server_url)
        self._audio_cfg = AudioCfg(cfg.audio.backend, cfg.audio.playout_ms)
        self._enable_hotkeys = enable_hotkeys
        self._hub_factory = hub_factory
        self._hotkeys: HotkeyListener | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._listeners: list[Callable[[], None]] = []

    # ------------------------------------------------------------------ life cycle

    def start(self, timeout: float = 20.0) -> None:
        """Start in the background and wait until the audio is open (or failed: see `error`)."""
        if self._thread:
            return
        self.error = None
        self._ready.clear()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), name="client-engine", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)

    def stop(self, timeout: float = 20.0) -> None:
        if self._thread is None:
            return
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout)
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self.error is None

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        http = None
        try:
            hub = self.hub = self._hub_factory(self.cfg.audio)
            hub.global_mute = self._global_mute
            hub.set_dim(self.cfg.dim)
            if (self.mic_gain_db, self.echo, self.noise, self.agc) != (
                    self.cfg.audio.mic_gain_db, self.cfg.audio.echo_cancellation, self.cfg.audio.noise_suppression,
                    self.cfg.audio.auto_gain):  # changed before the audio was open
                hub.set_mic_gain_db(self.mic_gain_db)
                hub.set_processing(self.echo, self.noise, self.agc)
            try:
                await hub.start()
            except DeviceError as e:
                self.error = str(e)
                log.error("audio: %s", e)
                return
            finally:
                self._ready.set()
            http = self._http = await create_client(self.cfg.server_url)
            self._auth = AuthClient(http, self.cfg.username, self.cfg.password)

            def factory(line_cfg: LineCfg, audio: AudioCfg, loop: asyncio.AbstractEventLoop) -> SharedLineAudio:
                return SharedLineAudio(self.controls[line_cfg.name], hub, loop, line_cfg.playout_ms or audio.playout_ms)

            self._factory = factory
            for line in self.cfg.lines:
                self._locks[line.name] = asyncio.Lock()
                if self.line_enabled[line.name]:
                    self._spawn(line)
            self._start_hotkeys()
            self._notify()
            sync = asyncio.ensure_future(self._sync_program_flags())
            companion = None
            if self.cfg.companion_url:
                self._companion = CompanionLink(self, self.cfg.companion_url)
                companion = asyncio.ensure_future(self._companion.run())
            await self._stop.wait()
            sync.cancel()
            if companion:
                companion.cancel()
                await asyncio.gather(companion, return_exceptions=True)
        finally:
            self._closing = True
            self._stop_hotkeys()
            for w in self.workers.values():
                w.stop()
            tasks = list(self._tasks.values())
            if tasks:
                done, pending = await asyncio.wait(tasks, timeout=15)
                for t in pending:
                    t.cancel()
            if self.hub:
                await self.hub.stop()
            if http:
                await http.aclose()
            self._ready.set()
            self._notify()

    async def _sync_program_flags(self) -> None:
        """The server says which line is the programme line (with the participants poll); the audio hub needs it
        to know which line to dim."""
        while True:
            for name, w in list(self.workers.items()):
                flag = getattr(w, "program_line", None)
                if flag is not None and name in self.controls:
                    self.controls[name].program = bool(flag)
            await asyncio.sleep(1.0)

    # ------------------------------------------------------------------ one line at a time

    def _spawn(self, line: ClientLine) -> None:
        """Start a (new) reconnecting worker for the line. On the engine's loop."""
        w = LineWorker(worker_line_cfg(self.cfg, line), self._server, self._audio_cfg, self._factory, self._http,
                       auth=self._auth)
        self.workers[line.name] = w
        self._tasks[line.name] = asyncio.ensure_future(w.run())

    async def _set_line(self, name: str, on: bool) -> None:
        async with self._locks[name]:
            line = next(l for l in self.cfg.lines if l.name == name)
            task = self._tasks.get(name)
            if on:
                if not self._closing and (task is None or task.done()):
                    self._spawn(line)
            else:
                self._tasks.pop(name, None)
                w = self.workers.pop(name, None)
                if w:
                    w.stop()
                if task:
                    # a connected line ends at once; one still logging in or connecting is cancelled after a moment
                    _, pending = await asyncio.wait({task}, timeout=2)
                    if pending:
                        task.cancel()
                        await asyncio.wait({task}, timeout=12)
                self.controls[name].talk.stop_all()  # nothing may stay "talking" into a line that is gone
        self._notify()

    def _run_on_loop(self, coro) -> None:
        if self._loop and self._thread and self._thread.is_alive():
            asyncio.run_coroutine_threadsafe(coro, self._loop)
        else:
            coro.close()

    def connect_line(self, name: str) -> None:
        self.line_enabled[name] = True
        self._run_on_loop(self._set_line(name, True))
        self._notify()

    def disconnect_line(self, name: str) -> None:
        self.line_enabled[name] = False
        self.controls[name].talk.stop_all()
        self._run_on_loop(self._set_line(name, False))
        self._notify()

    def toggle_line(self, name: str) -> None:
        if self.line_enabled[name]:
            self.disconnect_line(name)
        else:
            self.connect_line(name)

    # ------------------------------------------------------------------ hotkeys

    def _start_hotkeys(self) -> None:
        bindings = self.cfg.hotkey_bindings()
        if not self._enable_hotkeys or not bindings:
            self.hotkey_status = "off"
            return
        try:
            self._hotkeys = HotkeyListener(bindings, self.handle_hotkey)
            self._hotkeys.start()
            self.hotkey_status = "ok"
        except HotkeyError as e:
            self._hotkeys = None
            self.hotkey_status = str(e)
            log.warning("%s", e)

    def _stop_hotkeys(self) -> None:
        if self._hotkeys:
            self._hotkeys.stop()
            self._hotkeys = None

    def handle_hotkey(self, action: str, edge: str) -> None:
        """action: 'ptt:<line>', 'latch:<line>' or 'global_mute'; edge: 'down' or 'up'."""
        kind, _, name = action.partition(":")
        if kind == "ptt" and name in self.controls:
            self.ptt(name, edge == "down")
        elif edge == "down" and kind == "latch" and name in self.controls:
            self.toggle_latch(name)
        elif edge == "down" and kind == "global_mute":
            self.toggle_global_mute()

    # ------------------------------------------------------------------ what a person can do

    def on_change(self, listener: Callable[[], None]) -> None:
        """Called (from any thread) when something the Companion/Stream Deck view shows may have changed."""
        self._listeners.append(listener)

    def _notify(self) -> None:
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:
                log.exception("change listener failed")

    def can_talk(self, name: str) -> bool:
        """The programme line is for listening: nobody talks into it from here."""
        return not self.controls[name].program

    def ptt(self, name: str, down: bool) -> None:
        talk = self.controls[name].talk
        if down:
            if not self.can_talk(name):
                return
            talk.press()
        else:
            talk.release()
        self._notify()

    def stop_talking(self, name: str) -> None:
        """The microphone of this line is off, whether it was held or latched."""
        self.controls[name].talk.stop_all()
        self._notify()

    def toggle_latch(self, name: str) -> None:
        talk = self.controls[name].talk
        if not talk.latched and not self.can_talk(name):
            return
        talk.toggle_latch()
        self._notify()

    def set_mic_gain_db(self, db: float) -> None:
        self.mic_gain_db = max(-40.0, min(40.0, float(db)))
        if self.hub:
            self.hub.set_mic_gain_db(self.mic_gain_db)

    def set_processing(self, echo: bool | None = None, noise: int | None = None, agc: bool | None = None) -> None:
        """Change echo cancellation / noise suppression / automatic gain without reconnecting any line."""
        if echo is not None:
            self.echo = bool(echo)
        if noise is not None:
            self.noise = max(0, min(4, int(noise)))
        if agc is not None:
            self.agc = bool(agc)
        if self.hub:
            self.hub.set_processing(self.echo, self.noise, self.agc)

    def set_volume(self, name: str, pct: int) -> None:
        self.controls[name].volume_pct = max(0, min(150, int(pct)))
        self._notify()

    def set_listen_mute(self, name: str, mute: bool) -> None:
        self.controls[name].listen_mute = bool(mute)
        self._notify()

    def toggle_listen_mute(self, name: str) -> None:
        c = self.controls[name]
        self.set_listen_mute(name, not c.listen_mute)

    def set_global_mute(self, mute: bool) -> None:
        self._global_mute = bool(mute)
        if self.hub:
            self.hub.global_mute = self._global_mute
        self._notify()

    def toggle_global_mute(self) -> None:
        self.set_global_mute(not self.global_mute)

    @property
    def global_mute(self) -> bool:
        return self._global_mute

    def stop_talking_everywhere(self) -> None:
        """Safety: nothing is transmitting any more (hotkeys lost, window locked, ...)."""
        for c in self.controls.values():
            c.talk.stop_all()
        self._notify()

    # ------------------------------------------------------------------ what the UI shows

    def snapshot(self) -> dict:
        lines = []
        for name in self.order:
            c = self.controls[name]
            line = next(l for l in self.cfg.lines if l.name == name)
            enabled = self.line_enabled[name]
            w = self.workers.get(name) if enabled else None
            s = w.status() if w else {}
            lines.append({
                "name": name,
                "production_id": line.production_id,
                "line_id": line.line_id,
                "enabled": enabled,
                "state": s.get("state", "idle") if enabled else "off",
                "uptime_s": s.get("uptime_s", 0),
                "last_error": s.get("last_error"),
                "participants": s.get("participants"),
                "program": c.program,
                "speaking": bool(enabled and self.hub and self.hub.line_speaking(name)),
                "dimmed": bool(c.program and self.hub and self.hub.dimmed),
                "talking": c.talk.transmitting and not self.global_mute,
                "held": c.talk.held,
                "latched": c.talk.latched,
                "listen_mute": c.listen_mute,
                "volume_pct": c.volume_pct,
                "in_dbfs": s.get("in_dbfs", -120.0),
                "out_dbfs": s.get("out_dbfs", -120.0),
                "ptt_key": line.ptt_key,
                "latch_key": line.latch_key,
                "audio": s.get("audio", {}),
            })
        return {
            "running": self.running,
            "error": self.error,
            "global_mute": self.global_mute,
            "companion": self._companion.status if self._companion else "off",
            "mic_gain_db": self.mic_gain_db,
            "echo": self.echo,
            "noise": self.noise,
            "agc": self.agc,
            "dimmed": bool(self.hub and self.hub.dimmed),
            "dim_db": self.cfg.dim.amount_db,
            "processing": self.hub.processing if self.hub else "off",
            "mic_dbfs": self.hub.mic_level.dbfs if self.hub else -120.0,
            "out_dbfs": self.hub.out_level.dbfs if self.hub else -120.0,
            "hotkeys": self.hotkey_status,
            "global_mute_key": self.cfg.global_mute_key,
            "time": time.time(),
            "lines": lines,
        }
