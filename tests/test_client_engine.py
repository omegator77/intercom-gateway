"""The engine with a fake line worker: no network, the synthetic audio hub."""
import asyncio
import time

import numpy as np
import pytest

import intercom_client.engine as engine_mod
from gateway.audio import DeviceError
from intercom_client.config import parse_client_config
from intercom_client.engine import ClientEngine, worker_line_cfg
from intercom_client.hub import AudioHub


class FakeHttp:
    async def aclose(self):
        pass


class FakeWorker:
    instances: list = []

    def __init__(self, cfg, server, audio_cfg, factory, http, debug=None, auth=None):
        self.cfg, self.auth, self._factory, self._audio_cfg = cfg, auth, factory, audio_cfg
        self._stop = asyncio.Event()
        self.audio = None
        self.state = "connected"
        FakeWorker.instances.append(self)

    async def run(self):
        self.audio = self._factory(self.cfg, self._audio_cfg, asyncio.get_running_loop())
        await self.audio.start()
        await self._stop.wait()
        await self.audio.stop()

    def stop(self):
        self._stop.set()

    def status(self):
        return {"state": self.state, "uptime_s": 7, "last_error": None, "in_dbfs": -40.0, "out_dbfs": -50.0,
                "participants": [{"name": "Ben", "active": True, "whip": False}],
                "audio": {"underruns": 0}}


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    FakeWorker.instances = []

    async def fake_client(url):
        return FakeHttp()

    monkeypatch.setattr(engine_mod, "LineWorker", FakeWorker)
    monkeypatch.setattr(engine_mod, "create_client", fake_client)


CFG = {
    "server": {"url": "https://intercom.example", "username": "anna"},
    "audio": {"backend": "synthetic", "playout_ms": 80},
    "hotkeys": {"global_mute": "ctrl+alt+m"},
    "line": [{"name": "Regie", "production_id": "4", "line_id": "1", "ptt_key": "f1", "volume": 90},
             {"name": "Technik", "production_id": "4", "line_id": "2", "latch_key": "f2"}],
}


def make(**kw):
    cfg = parse_client_config(CFG, env={"INTERCOM_CLIENT_PASSWORD": "pw"})
    return cfg, ClientEngine(cfg, enable_hotkeys=False, **kw)


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_worker_config_uses_one_account_for_every_line():
    cfg, _ = make()
    w = worker_line_cfg(cfg, cfg.lines[0])
    assert (w.username, w.password, w.name) == ("anna", "pw", "Regie")
    assert (w.production_id, w.line_id, w.playout_ms) == ("4", "1", 80)


def test_start_creates_one_worker_per_line_sharing_one_login_and_stop_cleans_up():
    cfg, eng = make()
    eng.start()
    try:
        assert eng.running and eng.error is None
        assert wait_for(lambda: len(FakeWorker.instances) == 2 and all(w.audio for w in FakeWorker.instances))
        a, b = FakeWorker.instances
        assert a.auth is b.auth and a.auth is not None  # one login for all lines
        assert [w.cfg.name for w in FakeWorker.instances] == ["Regie", "Technik"]
        assert len(eng.hub._lines) == 2
    finally:
        eng.stop()
    assert not eng.running
    assert eng.hub._lines == ()  # every line detached


def test_controls_reach_the_audio_and_the_snapshot():
    cfg, eng = make()
    eng.start()
    try:
        assert wait_for(lambda: len(eng.hub._lines) == 2)
        regie = next(l for l in eng.hub._lines if l.control.name == "Regie")

        snap = eng.snapshot()
        r, t = snap["lines"]
        assert (r["name"], r["volume_pct"], r["talking"], r["state"]) == ("Regie", 90, False, "connected")
        assert r["participants"] == [{"name": "Ben", "active": True, "whip": False}]
        assert (r["ptt_key"], t["latch_key"], snap["global_mute_key"]) == ("f1", "f2", "ctrl+alt+m")

        eng.ptt("Regie", True)
        assert eng.snapshot()["lines"][0]["talking"] and eng.snapshot()["lines"][0]["held"]
        eng.ptt("Regie", False)
        assert not eng.snapshot()["lines"][0]["talking"]

        eng.toggle_latch("Regie")
        assert eng.snapshot()["lines"][0]["latched"] and eng.snapshot()["lines"][0]["talking"]
        eng.set_global_mute(True)
        assert eng.snapshot()["global_mute"] and not eng.snapshot()["lines"][0]["talking"]
        eng.toggle_global_mute()
        assert not eng.snapshot()["global_mute"] and eng.snapshot()["lines"][0]["talking"]

        eng.set_volume("Regie", 500)
        eng.set_volume("Technik", -5)
        assert (eng.controls["Regie"].volume_pct, eng.controls["Technik"].volume_pct) == (150, 0)
        eng.toggle_listen_mute("Technik")
        assert eng.snapshot()["lines"][1]["listen_mute"] and regie.control.gain == 1.5

        eng.stop_talking_everywhere()
        assert not eng.snapshot()["lines"][0]["talking"]
        assert eng.snapshot()["lines"][1]["state"] == "connected" and eng.snapshot()["running"]
    finally:
        eng.stop()


def test_hotkey_actions_drive_the_same_controls():
    cfg, eng = make()
    eng.handle_hotkey("ptt:Regie", "down")
    assert eng.controls["Regie"].talk.held
    eng.handle_hotkey("ptt:Regie", "up")
    assert not eng.controls["Regie"].talk.held
    eng.handle_hotkey("latch:Technik", "down")
    eng.handle_hotkey("latch:Technik", "up")  # only the press toggles
    assert eng.controls["Technik"].talk.latched
    eng.handle_hotkey("global_mute", "down")
    assert eng.global_mute
    eng.handle_hotkey("ptt:Unknown", "down")  # a stale binding must not crash


def test_global_mute_set_before_the_audio_is_open_still_applies():
    cfg, eng = make()
    eng.set_global_mute(True)
    eng.start()
    try:
        assert wait_for(lambda: eng.hub is not None and eng.hub.global_mute)
    finally:
        eng.stop()


def test_a_missing_sound_device_is_reported_instead_of_hanging():
    class BrokenHub(AudioHub):
        async def start(self):
            raise DeviceError("no microphone found - choose one in the settings")

        async def stop(self):
            pass

    cfg, eng = make(hub_factory=BrokenHub)
    eng.start()
    try:
        assert eng.error == "no microphone found - choose one in the settings"
        assert not eng.running
        assert FakeWorker.instances == []  # no lines are connected without audio
        assert eng.snapshot()["error"] == eng.error
    finally:
        eng.stop()


def test_listeners_hear_about_changes():
    cfg, eng = make()
    calls = []
    eng.on_change(lambda: calls.append(1))
    eng.ptt("Regie", True)
    eng.set_volume("Regie", 70)
    eng.toggle_global_mute()
    assert len(calls) == 3
    eng.on_change(lambda: 1 / 0)  # a broken listener must not break the controls
    eng.toggle_latch("Regie")
    assert len(calls) == 4


def test_one_line_can_be_disconnected_and_connected_again_while_the_others_stay():
    cfg, eng = make()
    eng.start()
    try:
        assert wait_for(lambda: len(FakeWorker.instances) == 2 and len(eng.hub._lines) == 2)
        eng.ptt("Regie", True)
        eng.toggle_latch("Regie")

        eng.disconnect_line("Regie")
        assert wait_for(lambda: len(eng.hub._lines) == 1)
        assert [l.control.name for l in eng.hub._lines] == ["Technik"]  # Regie's audio was released
        snap = eng.snapshot()["lines"]
        assert (snap[0]["name"], snap[0]["enabled"], snap[0]["state"]) == ("Regie", False, "off")
        assert snap[1]["enabled"] and snap[1]["state"] == "connected"
        assert not eng.controls["Regie"].talk.transmitting  # nothing keeps "talking" into a line that is gone
        assert FakeWorker.instances[0]._stop.is_set()

        eng.connect_line("Regie")
        assert wait_for(lambda: len(eng.hub._lines) == 2 and len(FakeWorker.instances) == 3)
        assert eng.snapshot()["lines"][0]["enabled"] and eng.snapshot()["lines"][0]["state"] == "connected"
        assert FakeWorker.instances[2].auth is FakeWorker.instances[1].auth  # still the one shared login

        eng.toggle_line("Technik")
        assert wait_for(lambda: not eng.snapshot()["lines"][1]["enabled"])
        eng.toggle_line("Technik")
        assert wait_for(lambda: eng.snapshot()["lines"][1]["enabled"] and len(eng.hub._lines) == 2)
    finally:
        eng.stop()
    assert eng.hub._lines == ()


def test_a_line_that_is_still_connecting_is_cancelled_when_disconnected(monkeypatch):
    class Stuck(FakeWorker):
        async def run(self):
            if self.cfg.name == "Regie":
                await asyncio.sleep(3600)  # never gets connected (login or connect hanging)
            else:
                await super().run()

    monkeypatch.setattr(engine_mod, "LineWorker", Stuck)
    cfg, eng = make()
    eng.start()
    try:
        assert wait_for(lambda: len(eng.hub._lines) == 1)
        started = time.monotonic()
        eng.disconnect_line("Regie")
        assert wait_for(lambda: "Regie" not in eng._tasks, 5)
        assert wait_for(lambda: eng.snapshot()["lines"][0]["state"] == "off", 5)
        assert time.monotonic() - started < 6  # not the full hour, not even the connect timeout
    finally:
        eng.stop()


def test_line_commands_before_start_do_not_crash():
    cfg, eng = make()
    eng.disconnect_line("Regie")
    eng.connect_line("Regie")
    eng.toggle_line("Technik")
    assert eng.line_enabled == {"Regie": True, "Technik": False}
