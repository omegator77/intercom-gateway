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
        tasks: list[asyncio.Task] = []
        try:
            hub = self.hub = self._hub_factory(self.cfg.audio)
            hub.global_mute = self._global_mute
            try:
                await hub.start()
            except DeviceError as e:
                self.error = str(e)
                log.error("audio: %s", e)
                return
            finally:
                self._ready.set()
            http = await create_client(self.cfg.server_url)
            auth = AuthClient(http, self.cfg.username, self.cfg.password)
            audio_cfg = AudioCfg(self.cfg.audio.backend, self.cfg.audio.playout_ms)
            server = ServerCfg(self.cfg.server_url)

            def factory(line_cfg: LineCfg, audio: AudioCfg, loop: asyncio.AbstractEventLoop) -> SharedLineAudio:
                return SharedLineAudio(self.controls[line_cfg.name], hub, loop, line_cfg.playout_ms or audio.playout_ms)

            for line in self.cfg.lines:
                w = LineWorker(worker_line_cfg(self.cfg, line), server, audio_cfg, factory, http, auth=auth)
                self.workers[line.name] = w
                tasks.append(asyncio.ensure_future(w.run()))
            self._start_hotkeys()
            self._notify()
            await self._stop.wait()
        finally:
            self._stop_hotkeys()
            for w in self.workers.values():
                w.stop()
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

    def ptt(self, name: str, down: bool) -> None:
        talk = self.controls[name].talk
        if down:
            talk.press()
        else:
            talk.release()
        self._notify()

    def toggle_latch(self, name: str) -> None:
        self.controls[name].talk.toggle_latch()
        self._notify()

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
            w = self.workers.get(name)
            s = w.status() if w else {}
            lines.append({
                "name": name,
                "production_id": line.production_id,
                "line_id": line.line_id,
                "state": s.get("state", "idle"),
                "uptime_s": s.get("uptime_s", 0),
                "last_error": s.get("last_error"),
                "participants": s.get("participants"),
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
            "mic_dbfs": self.hub.mic_level.dbfs if self.hub else -120.0,
            "out_dbfs": self.hub.out_level.dbfs if self.hub else -120.0,
            "hotkeys": self.hotkey_status,
            "global_mute_key": self.cfg.global_mute_key,
            "time": time.time(),
            "lines": lines,
        }
