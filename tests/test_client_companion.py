"""The Companion / Stream Deck link, against a stand-in for the Eyevinn Companion module (a WebSocket server that
accepts one client and answers a second one with 409, like the module's source does)."""
import asyncio
import json
import threading
import time

import numpy as np
import pytest

import intercom_client.companion as companion_mod
import intercom_client.engine as engine_mod
from intercom_client.companion import CompanionLink, build_state
from intercom_client.config import parse_client_config
from intercom_client.engine import ClientEngine


# ---------------------------------------------------------------- the stand-in for the module

class FakeModule:
    def __init__(self):
        self.received: list[dict] = []
        self.clients: set = set()
        self.refused = 0
        self._loop = None
        self._server = None
        self._ready = threading.Event()
        self.port = None

    def start(self, port: int = 0) -> int:
        threading.Thread(target=lambda: asyncio.run(self._main(port)), daemon=True).start()
        assert self._ready.wait(5)
        return self.port

    async def _main(self, port):
        from websockets.asyncio.server import serve

        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()

        async def handler(ws):
            if self.clients:  # the module accepts one client only
                self.refused += 1
                await ws.send(json.dumps({"type": "error", "message": "Another client is connected", "statusCode": 409}))
                await ws.close()
                return
            self.clients.add(ws)
            try:
                async for message in ws:
                    self.received.append(json.loads(message))
            finally:
                self.clients.discard(ws)

        async with serve(handler, "127.0.0.1", port) as server:
            self._server = server
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._stop.wait()

    def stop(self):
        if self._loop:
            self._loop.call_soon_threadsafe(self._stop.set)

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(5)

    def press(self, action: str, index=None):
        """A Stream Deck button."""
        msg = {"action": action} if index is None else {"action": action, "index": index}

        async def send():
            for ws in list(self.clients):
                await ws.send(json.dumps(msg))

        self._run(send())

    def drop(self):
        async def close():
            for ws in list(self.clients):
                await ws.close()

        self._run(close())

    @property
    def states(self) -> list[dict]:
        return [m for m in self.received if m.get("type") == "CALLS_STATE_UPDATE"]

    def last_call(self, index: int) -> dict:
        return next(c for c in self.states[-1]["calls"] if c["index"] == index)


def wait_for(cond, timeout=6.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if cond():
                return True
        except (IndexError, StopIteration, KeyError):
            pass
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------- an engine with fake workers

class FakeWorker:
    def __init__(self, cfg, server, audio_cfg, factory, http, debug=None, auth=None):
        self.cfg, self._factory, self._audio_cfg = cfg, factory, audio_cfg
        self._stop = asyncio.Event()
        self.audio = None
        self.program_line = cfg.name == "Programm"

    async def run(self):
        self.audio = self._factory(self.cfg, self._audio_cfg, asyncio.get_running_loop())
        await self.audio.start()
        await self._stop.wait()
        await self.audio.stop()

    def stop(self):
        self._stop.set()

    def status(self):
        return {"state": "connected", "uptime_s": 5, "last_error": None, "in_dbfs": -40.0, "out_dbfs": -50.0,
                "participants": [], "audio": {}, "program_line": self.program_line}


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    class FakeHttp:
        async def aclose(self):
            pass

    async def fake_client(url):
        return FakeHttp()

    monkeypatch.setattr(engine_mod, "LineWorker", FakeWorker)
    monkeypatch.setattr(engine_mod, "create_client", fake_client)
    monkeypatch.setattr(companion_mod, "POLL_S", 0.02)
    monkeypatch.setattr(companion_mod, "RETRY_MIN_S", 0.15)
    monkeypatch.setattr(companion_mod, "RETRY_MAX_S", 0.3)
    monkeypatch.setattr(companion_mod, "BUSY_RETRY_S", 0.3)


@pytest.fixture
def module():
    m = FakeModule()
    m.start()
    yield m
    m.stop()


def make_engine(url, **extra):
    data = {
        "server": {"url": "https://intercom.example", "username": "anna"},
        "audio": {"backend": "synthetic"},
        "companion": {"url": url} if url else {},
        "line": [{"name": "Regie", "production_id": "4", "line_id": "1"},
                 {"name": "Technik", "production_id": "4", "line_id": "2", "volume": 80},
                 {"name": "Programm", "production_id": "4", "line_id": "3"}],
        **extra,
    }
    cfg = parse_client_config(data, env={"INTERCOM_CLIENT_PASSWORD": "pw"})
    return ClientEngine(cfg, enable_hotkeys=False)


@pytest.fixture
def engine(module):
    eng = make_engine(f"ws://127.0.0.1:{module.port}")
    eng.start()
    yield eng
    eng.stop()


# ---------------------------------------------------------------- the state message

def test_build_state_has_what_the_module_reads():
    snap = {"global_mute": True, "lines": [
        {"name": "Regie", "line_id": "1", "production_id": "4", "talking": True, "listen_mute": False,
         "volume_pct": 90, "program": False, "speaking": True, "enabled": True},
        {"name": "Programm", "line_id": "3", "production_id": "4", "talking": False, "listen_mute": True,
         "volume_pct": 100, "program": True, "speaking": True, "enabled": True},
        {"name": "Aus", "line_id": "9", "production_id": "4", "talking": True, "listen_mute": False,
         "volume_pct": 100, "program": False, "speaking": True, "enabled": False}]}
    s = build_state(snap)
    assert (s["type"], s["globalMute"], s["numberOfCalls"]) == ("CALLS_STATE_UPDATE", True, 3)
    r, p, off = s["calls"]
    assert (r["index"], r["lineName"], r["isInputMuted"], r["isOutputMuted"], r["volume"]) == (1, "Regie", False, False, 0.9)
    assert r["isSomeoneSpeaking"] is True and r["isProgramOutputLine"] is False
    assert (p["index"], p["isOutputMuted"], p["isProgramOutputLine"]) == (2, True, True)
    assert off["isInputMuted"] and off["isOutputMuted"] and not off["isSomeoneSpeaking"]  # a disconnected line looks muted
    assert json.loads(json.dumps(s)) == s  # plain JSON


# ---------------------------------------------------------------- against the module

def test_the_state_arrives_and_follows_the_client(module, engine):
    assert wait_for(lambda: module.states and len(module.states[-1]["calls"]) == 3)
    first = module.states[0]
    assert [c["lineName"] for c in first["calls"]] == ["Regie", "Technik", "Programm"]
    assert [c["index"] for c in first["calls"]] == [1, 2, 3]
    assert first["globalMute"] is False and all(c["isInputMuted"] for c in first["calls"])
    assert module.last_call(2)["volume"] == 0.8
    assert engine.snapshot()["companion"] == "connected"

    engine.ptt("Regie", True)
    assert wait_for(lambda: not module.last_call(1)["isInputMuted"])
    engine.ptt("Regie", False)
    assert wait_for(lambda: module.last_call(1)["isInputMuted"])

    engine.set_global_mute(True)
    assert wait_for(lambda: module.states[-1]["globalMute"] is True)

    n = len(module.states)
    time.sleep(0.3)
    assert len(module.states) == n  # nothing changed, nothing is sent


def test_somebody_speaking_to_me_and_the_programme_line_show_up(module, engine):
    assert wait_for(lambda: engine.hub is not None and len(engine.hub._lines) == 3)
    assert wait_for(lambda: engine.controls["Programm"].program)
    regie = next(l for l in engine.hub._lines if l.control.name == "Regie")
    prog = next(l for l in engine.hub._lines if l.control.name == "Programm")
    for la in (regie, prog):  # remote speech arrives on Regie and on the programme line
        for _ in range(30):
            la.on_remote_pcm(np.full(480, 0.2, np.float32), 1)
            la.out_buf.read(480)
            la.raw_level.update(np.full(480, 0.2, np.float32))
    assert wait_for(lambda: module.last_call(1)["isSomeoneSpeaking"])
    assert module.last_call(3)["isProgramOutputLine"] is True and module.last_call(3)["isSomeoneSpeaking"] is False


def test_the_buttons_control_the_client(module, engine):
    assert wait_for(lambda: module.states)
    module.press("push_to_talk_start", 1)
    assert wait_for(lambda: engine.controls["Regie"].talk.held)
    module.press("push_to_talk_stop", 1)
    assert wait_for(lambda: not engine.controls["Regie"].talk.held)

    module.press("toggle_input_mute", 2)  # unmute: talk until muted again
    assert wait_for(lambda: engine.controls["Technik"].talk.latched)
    module.press("toggle_input_mute", 2)
    assert wait_for(lambda: not engine.controls["Technik"].talk.transmitting)

    module.press("toggle_output_mute", 3)
    assert wait_for(lambda: engine.controls["Programm"].listen_mute)
    module.press("toggle_output_mute", 3)
    assert wait_for(lambda: not engine.controls["Programm"].listen_mute)

    module.press("increase_volume", 1)
    assert wait_for(lambda: engine.controls["Regie"].volume_pct == 105)
    module.press("decrease_volume", 2)
    module.press("decrease_volume", 2)
    assert wait_for(lambda: engine.controls["Technik"].volume_pct == 70)

    module.press("toggle_global_mute")  # no line number needed
    assert wait_for(lambda: engine.global_mute)
    module.press("toggle_global_mute")
    assert wait_for(lambda: not engine.global_mute)


def test_bad_buttons_are_ignored_and_do_not_break_the_link(module, engine):
    assert wait_for(lambda: module.states)
    for action, index in (("push_to_talk_start", 0), ("push_to_talk_start", 4), ("push_to_talk_start", None),
                          ("push_to_talk_start", "x"), ("explode", 1), ("open_intercom", 1)):
        module.press(action, index)
    module.press("toggle_input_mute", 1)  # a good one afterwards proves the link still works
    assert wait_for(lambda: engine.controls["Regie"].talk.latched)
    assert not any(c.talk.held for c in engine.controls.values())
    assert engine.snapshot()["companion"] == "connected"


def test_a_disconnected_line_ignores_its_buttons(module, engine):
    assert wait_for(lambda: len(engine.hub._lines) == 3)
    engine.disconnect_line("Technik")
    assert wait_for(lambda: module.last_call(2)["isInputMuted"] and module.last_call(2)["isOutputMuted"])
    module.press("toggle_input_mute", 2)
    module.press("toggle_input_mute", 1)  # a later button on a connected line is processed after the ignored one
    assert wait_for(lambda: engine.controls["Regie"].talk.latched)
    assert not engine.controls["Technik"].talk.transmitting


def test_a_held_button_lets_go_when_the_link_drops_and_the_link_comes_back(module, engine):
    assert wait_for(lambda: module.states)
    module.press("push_to_talk_start", 1)
    assert wait_for(lambda: engine.controls["Regie"].talk.held)
    before = len(module.states)
    module.drop()
    assert wait_for(lambda: not engine.controls["Regie"].talk.held)  # nothing stays "talking" without its button
    assert wait_for(lambda: len(module.states) > before and engine.snapshot()["companion"] == "connected")
    assert len(module.states[-1]["calls"]) == 3  # the full state is sent again


def test_a_second_client_is_refused_like_the_module_does_and_tries_again_later(module):
    from websockets.sync.client import connect

    eng = make_engine(f"ws://127.0.0.1:{module.port}")
    try:
        with connect(f"ws://127.0.0.1:{module.port}"):  # somebody else (the web app) is connected first
            assert wait_for(lambda: module.clients)
            eng.start()
            assert wait_for(lambda: eng.snapshot()["companion"] == "busy")
            assert module.refused >= 1 and module.states == []
        # the other client is gone: the link gets in on its next try
        assert wait_for(lambda: eng.snapshot()["companion"] == "connected" and module.states, 8)
    finally:
        eng.stop()


def test_without_a_module_running_the_link_keeps_trying_and_connects_when_it_appears():
    probe = FakeModule()
    port = probe.start()
    probe.stop()
    time.sleep(0.3)  # the port is free again, and nobody listens on it
    eng = make_engine(f"ws://127.0.0.1:{port}")
    eng.start()
    try:
        assert wait_for(lambda: eng.snapshot()["companion"].startswith("error"))
        assert eng.running  # the audio and the lines do not care
        module = FakeModule()
        module.start(port)
        try:
            assert wait_for(lambda: eng.snapshot()["companion"] == "connected" and module.states, 8)
        finally:
            module.stop()
    finally:
        eng.stop()


def test_companion_is_off_unless_configured():
    eng = make_engine(None)
    eng.start()
    try:
        assert eng.snapshot()["companion"] == "off"
    finally:
        eng.stop()


# ---------------------------------------------------------------- the link without a socket

class StubEngine:
    def __init__(self):
        from intercom_client.hub import LineControl

        self.order = ["A", "B"]
        self.controls = {n: LineControl(n) for n in self.order}
        self.line_enabled = {"A": True, "B": True}
        self.calls = []
        self.global_mute = False

    def can_talk(self, name):
        return not self.controls[name].program

    def ptt(self, name, down):
        self.calls.append(("ptt", name, down))

    def toggle_global_mute(self):
        self.calls.append(("global",))


def test_message_parsing_is_forgiving():
    eng = StubEngine()
    link = CompanionLink(eng, "ws://x")
    for junk in ("", "not json", "[1,2]", "42", "null", '{"action": 5}', '{"type": "error"}',
                 '{"type": "error", "statusCode": 500}'):
        link._on_message(junk)
    assert eng.calls == [] and not link._busy
    link._on_message(b'{"type": "error", "statusCode": 409, "message": "busy"}')
    assert link._busy
    link._on_message('{"action": "push_to_talk_start", "index": "2"}')  # the module may send the index as text
    assert eng.calls == [("ptt", "B", True)] and link._holds == {"B"}
    link._release_holds()
    assert eng.calls[-1] == ("ptt", "B", False) and link._holds == set()
