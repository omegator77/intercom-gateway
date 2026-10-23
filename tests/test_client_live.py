"""Settings changed while the client is connected: what applies live, and what needs a new connection."""
import asyncio
import copy

import pytest

from gateway.audio import DeviceError
from intercom_client.config import ClientAudio, parse_client_config
from intercom_client.hub import PortAudioHub
from test_client_engine import CFG, FakeWorker, fakes, make, wait_for  # noqa: F401  (fakes: the autouse fixture)


def changed(**edit):
    """The test configuration with some tables replaced / extended."""
    data = copy.deepcopy(CFG)
    for key, value in edit.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key].update(value)
        else:
            data[key] = value
    return parse_client_config(data, env={"INTERCOM_CLIENT_PASSWORD": "pw"})


def sessions(eng):
    return {name: id(w) for name, w in eng.workers.items()}


def test_settings_that_have_nothing_to_do_with_the_connection_do_not_reconnect_any_line():
    cfg, eng = make()
    eng.start()
    try:
        assert wait_for(lambda: len(FakeWorker.instances) == 2 and len(eng.hub._lines) == 2)
        before = sessions(eng)
        new = changed(audio={"backend": "synthetic", "playout_ms": 80, "mic_gain_db": 6.0, "echo_cancellation": True,
                             "noise_suppression": 2, "auto_gain": True, "agc_target_dbfs": -24, "agc_max_gain_db": 12,
                             "output_gain_db": -3.0},
                      dim={"amount_db": -20, "delay_ms": 500, "when_addressed": False},
                      hotkeys={"global_mute": "ctrl+alt+x"},
                      companion={"url": "ws://127.0.0.1:12345"})
        result = eng.apply_config(new)
        assert result.live and result.reason is None
        assert sessions(eng) == before and len(FakeWorker.instances) == 2  # not one line was touched
        assert eng.cfg is new
        assert (eng.mic_gain_db, eng.echo, eng.noise, eng.agc) == (6.0, True, 2, True)
        assert (eng.hub.cfg.agc_target_dbfs, eng.hub.cfg.agc_max_gain_db) == (-24, 12)
        assert eng.hub._dimmer.cfg.amount_db == -20 and eng.hub._dimmer.cfg.when_addressed is False
        assert eng.snapshot()["global_mute_key"] == "ctrl+alt+x" and eng.snapshot()["dim_db"] == -20
        assert eng.snapshot()["companion"] != "off"
        # and back: the Companion link goes away again, still without touching a line
        assert eng.apply_config(changed()).live
        assert eng.snapshot()["companion"] == "off" and sessions(eng) == before
    finally:
        eng.stop()


def test_a_new_server_or_login_needs_a_new_connection_and_apply_changes_nothing_by_itself():
    cfg, eng = make()
    eng.start()
    try:
        assert wait_for(lambda: len(eng.hub._lines) == 2)
        before = sessions(eng)
        other_server = changed(server={"url": "https://other.example", "username": "anna"})
        other_user = changed(server={"url": "https://intercom.example", "username": "ben"})
        for new in (other_server, other_user):
            result = eng.apply_config(new)
            assert not result.live and "login" in result.reason
        assert sessions(eng) == before and eng.cfg is cfg  # the caller connects again; apply itself did nothing
    finally:
        eng.stop()


def test_adding_removing_and_repointing_lines_only_touches_those_lines():
    cfg, eng = make()
    eng.start()
    try:
        assert wait_for(lambda: len(eng.hub._lines) == 2)
        before = sessions(eng)
        regie_task = eng._tasks["Regie"]
        lines = [dict(l) for l in CFG["line"]]
        lines[1]["line_id"] = "9"  # Technik now points at another line
        lines.append({"name": "Studio", "production_id": "5", "line_id": "3", "ptt_key": "f3"})
        assert eng.apply_config(changed(line=lines)).live
        assert wait_for(lambda: len(eng.hub._lines) == 3 and len(FakeWorker.instances) == 4)
        now = sessions(eng)
        assert now["Regie"] == before["Regie"] and eng._tasks["Regie"] is regie_task  # untouched
        assert now["Technik"] != before["Technik"] and "Studio" in now  # reconnected / new
        assert [w.cfg.line_id for w in FakeWorker.instances if w.cfg.name == "Technik"] == ["2", "9"]
        assert eng.snapshot()["lines"][2]["ptt_key"] == "f3" and eng.order == ["Regie", "Technik", "Studio"]
        # remove one: only it goes, and it can no longer be talked into
        eng.ptt("Studio", True)
        assert eng.apply_config(changed(line=lines[:2])).live
        assert wait_for(lambda: len(eng.hub._lines) == 2)
        assert "Studio" not in eng.controls and "Studio" not in eng.workers and eng.order == ["Regie", "Technik"]
        assert sessions(eng)["Regie"] == before["Regie"]
    finally:
        eng.stop()


def test_a_line_switched_off_stays_off_and_new_keys_apply_without_reconnecting():
    cfg, eng = make()
    eng.start()
    try:
        assert wait_for(lambda: len(eng.hub._lines) == 2)
        eng.disconnect_line("Technik")
        assert wait_for(lambda: len(eng.hub._lines) == 1)
        before = len(FakeWorker.instances)
        lines = [dict(l) for l in CFG["line"]]
        lines[0]["ptt_key"] = "f5"
        lines[1]["autoconnect"] = False  # what the file says after the card was switched off (persisted)
        assert eng.apply_config(changed(line=lines)).live
        assert len(FakeWorker.instances) == before and not eng.line_enabled["Technik"]
        assert eng.cfg.lines[0].ptt_key == "f5"
        # switching "connect at start" on in the settings connects that line, and only that one
        lines[1].pop("autoconnect")
        assert eng.apply_config(changed(line=lines)).live
        assert wait_for(lambda: len(eng.hub._lines) == 2 and len(FakeWorker.instances) == before + 1)
    finally:
        eng.stop()


def test_other_sound_devices_are_opened_without_touching_the_lines_and_a_failure_keeps_the_old_ones(monkeypatch):
    cfg, eng = make()
    eng.start()
    try:
        assert wait_for(lambda: len(eng.hub._lines) == 2)
        before = sessions(eng)
        opened = []

        async def good(self, audio):
            opened.append(audio.input_device)
            self.cfg = audio

        monkeypatch.setattr(type(eng.hub), "reopen", good)
        new = changed(audio={"backend": "synthetic", "playout_ms": 80, "input_device": "USB Headset"})
        result = eng.apply_config(new)
        assert result.live and result.reason is None and opened == ["USB Headset"]
        assert sessions(eng) == before

        async def bad(self, audio):
            raise DeviceError("no such device")

        monkeypatch.setattr(type(eng.hub), "reopen", bad)
        other = changed(audio={"backend": "synthetic", "playout_ms": 80, "input_device": "Gone"}, dim={"amount_db": -5})
        result = eng.apply_config(other)
        assert result.live and "old ones keep running" in result.reason and "no such device" in result.reason
        assert eng.hub._dimmer.cfg.amount_db == -5 and sessions(eng) == before  # the rest was applied anyway
    finally:
        eng.stop()


def test_apply_without_a_connection_asks_the_caller_to_connect():
    cfg, eng = make()
    assert not eng.apply_config(changed()).live


def test_the_portaudio_hub_keeps_its_old_devices_when_the_new_ones_do_not_open(monkeypatch):
    closed = []

    class Stream:
        def stop(self):
            closed.append("stop")

        def close(self):
            closed.append("close")

    async def go():
        hub = PortAudioHub(ClientAudio())
        old = (Stream(), Stream())
        hub._in, hub._out = old

        async def failing():
            await hub.stop()  # what start() does when the devices do not open: it lets go of what it had opened
            raise DeviceError("could not open the audio devices")

        monkeypatch.setattr(hub, "start", failing)
        with pytest.raises(DeviceError):
            await hub.reopen(ClientAudio(input_device="Nope"))
        assert (hub._in, hub._out) == old and hub.cfg.input_device is None and closed == []  # old streams still open

        async def working():
            hub._in, hub._out = Stream(), Stream()

        monkeypatch.setattr(hub, "start", working)
        await hub.reopen(ClientAudio(input_device="USB"))
        assert hub.cfg.input_device == "USB" and (hub._in, hub._out) != old and closed == ["stop", "close", "stop", "close"]

    asyncio.run(go())
