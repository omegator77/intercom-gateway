"""Accounts, intercom lines and call profiles managed at runtime: the store, the validation, applying changes to a running
service (and refusing them while a call is up), and the page's API for it - without ever handing out a password."""
import asyncio
import json
import time

import pytest

from intercom_sip import calls as calls_mod
from intercom_sip.admin import ConfigManager, clean
from intercom_sip.calls import CallState, SipService
from intercom_sip.config import ConfigError, WebCfg, merge_raw, parse_sip_config
from intercom_sip.store import Store, StoreError
from intercom_sip.web import WebServer
from test_sip_calls import FakeAudio, FakeRegistrar, attach, wait_for
from test_sip_control import http

NOPW = lambda url, user: None  # noqa: E731


@pytest.fixture(autouse=True)
def no_intercom(monkeypatch):
    async def nothing(self):
        return

    monkeypatch.setattr(calls_mod.CallRunner, "_start_lines", nothing)
    monkeypatch.setattr(calls_mod.CallRunner, "_stop_lines", nothing)


BASE = {"server": {"url": "http://127.0.0.1:1"},
        "sip": {"sip_port": 0, "local_ip": "127.0.0.1", "bind_ip": "127.0.0.1", "rtp_port_min": 41000, "rtp_port_max": 41060}}


def line_form(name, n=1, password="lp"):
    return {"name": name, "production_id": "1", "line_id": str(n), "username": name.lower(), "password": password}


def profile_form(name, **extra):
    form = {"name": name, "mode": "answer", "accept_from": "127.0.0.1", "codec": "mp2", "bitrate_kbps": "192",
            "channel_mode": "dual-mono", "left": {"send": "Regie", "receive": "Regie"}}
    form.update(extra)
    return form


async def make(tmp_path, toml=None, with_web=False):
    store = Store(tmp_path / "sip-data.json")
    data = dict(toml or BASE)
    cfg = parse_sip_config(merge_raw(data, store.raw()), lookup_password=NOPW, require_calls=False)
    service = SipService(cfg, None)
    await service.start()
    manager = ConfigManager(service, data, store, lookup_password=NOPW)
    return service, manager, store


# ---------------------------------------------------------------- the store and the form cleaning

def test_the_form_is_cleaned_to_a_stored_entry_and_an_empty_password_keeps_the_old_one():
    entry = clean("accounts", {"name": " ARD ", "registrar": "sip:s.example", "user": "4711", "password": "",
                               "domain": "", "expires_s": "120", "junk": "x"}, {"name": "ARD", "password": "old"})
    assert entry == {"name": "ARD", "registrar": "sip:s.example", "user": "4711", "expires_s": 120, "password": "old"}
    call = clean("calls", profile_form("A", target="", redial="true", in_gain_db="1,5", right={"send": "", "receive": ""}), None)
    assert call["accept_from"] == ["127.0.0.1"] and call["redial"] is True and call["in_gain_db"] == 1.5
    assert "target" not in call and "right" not in call and call["left"] == {"send": "Regie", "receive": "Regie"}
    with pytest.raises(StoreError, match="Nummer"):
        clean("accounts", {"name": "x", "registrar": "sip:s"}, None)
    with pytest.raises(StoreError, match="Passwort"):
        clean("lines", {"name": "x", "production_id": "1", "line_id": "1", "username": "u"}, None)
    with pytest.raises(StoreError, match="Ungültiger Wert"):
        clean("accounts", {"name": "x", "registrar": "s", "user": "1", "expires_s": "soon"}, None)


def test_the_store_survives_a_restart_is_written_for_its_owner_and_renames_follow_references(tmp_path):
    store = Store(tmp_path / "d" / "sip-data.json")
    store.put("accounts", {"name": "ARD", "registrar": "sip:s", "user": "1", "password": "pw"})
    store.put("lines", {"name": "Regie", "production_id": "1", "line_id": "1", "username": "u", "password": "p"})
    store.put("calls", {"name": "A", "account": "ARD", "mode": "both", "left": {"send": "Regie", "receive": "Regie"}})
    store.save()
    again = Store(tmp_path / "d" / "sip-data.json")
    again.load()
    assert again.names("accounts") == ["ARD"] and again.get("accounts", "ARD")["password"] == "pw"
    again.put("accounts", {"name": "ARD2", "registrar": "sip:s", "user": "1"}, old_name="ARD")
    again.put("lines", {"name": "Regie2", "production_id": "1", "line_id": "1", "username": "u", "password": "p"},
              old_name="Regie")
    call = again.get("calls", "A")
    assert call["account"] == "ARD2" and call["left"] == {"send": "Regie2", "receive": "Regie2"}
    with pytest.raises(StoreError, match="gibt es schon"):
        again.put("accounts", {"name": "ARD2"})
    (tmp_path / "d" / "sip-data.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(StoreError, match="nicht lesbar"):
        Store(tmp_path / "d" / "sip-data.json").load()


# ---------------------------------------------------------------- applying changes to a running service

def test_adding_changing_and_deleting_while_the_service_runs(tmp_path):
    async def go():
        service, manager, store = await make(tmp_path)
        run = asyncio.ensure_future(service.run())
        try:
            assert service.runners == [] and service.registrations == {}
            await manager.save("lines", None, line_form("Regie"))
            await manager.save("calls", None, profile_form("Studio"))
            r = service.runners[0]
            attach(service)
            assert r.cfg.name == "Studio" and await wait_for(lambda: r.state == CallState.WAITING)
            assert store.path.exists() and json.loads(store.path.read_text())["calls"][0]["name"] == "Studio"
            # a change replaces the profile (new object, new id), an untouched one keeps running
            await manager.save("lines", None, line_form("Technik", 2))
            await manager.save("calls", None, profile_form("Zweite", left={"send": "Technik", "receive": "Technik"}))
            first = service.runner(r.uid)
            assert first is r and r.state == CallState.WAITING  # adding another profile left the first alone
            await manager.save("calls", "Studio", profile_form("Studio", bitrate_kbps="128", channel_mode="stereo"))
            replaced = [x for x in service.runners if x.cfg.name == "Studio"][0]
            assert replaced is not r and replaced.cfg.fmt.bitrate_kbps == 128 and replaced.uid != r.uid
            assert service.runner(r.uid) is None
            with pytest.raises(StoreError, match="noch benutzt von: „Studio“"):
                await manager.delete("lines", "Regie")
            await manager.delete("calls", "Studio")
            await manager.delete("lines", "Regie")
            assert [x.cfg.name for x in service.runners] == ["Zweite"] and "Regie" not in service.cfg.lines
        finally:
            service.stop()
            await asyncio.wait_for(run, 15)
            await service.close()

    asyncio.run(go())


def test_a_bad_change_is_refused_and_leaves_everything_as_it_was(tmp_path):
    async def go():
        service, manager, store = await make(tmp_path)
        run = asyncio.ensure_future(service.run())
        try:
            await manager.save("lines", None, line_form("Regie"))
            await manager.save("calls", None, profile_form("Studio"))
            before = store.snapshot()
            with pytest.raises(StoreError, match="Ungültig.*100 kbit"):
                await manager.save("calls", "Studio", profile_form("Studio", bitrate_kbps="100"))
            with pytest.raises(StoreError, match="Ungültig.*unknown line"):
                await manager.save("calls", None, profile_form("Neu", left={"send": "Gibt-es-nicht"}))
            with pytest.raises(StoreError, match="gibt es schon"):
                await manager.save("calls", None, profile_form("Studio"))
            with pytest.raises(StoreError, match="gibt es nicht"):
                await manager.save("calls", "Nope", profile_form("Nope"))
            assert store.snapshot() == before and [r.cfg.name for r in service.runners] == ["Studio"]
            assert service.runners[0].cfg.fmt.bitrate_kbps == 192
            assert json.loads(store.path.read_text())["calls"][0]["bitrate_kbps"] == 192
        finally:
            service.stop()
            await asyncio.wait_for(run, 15)
            await service.close()

    asyncio.run(go())


def test_a_profile_in_a_call_cannot_be_changed_or_deleted(tmp_path):
    async def go():
        service, manager, store = await make(tmp_path)
        run = asyncio.ensure_future(service.run())
        saw = SipService(parse_sip_config({**BASE, "sip": {**BASE["sip"], "rtp_port_min": 41200, "rtp_port_max": 41260},
                                           "line": [{"name": "S", "production_id": "1", "line_id": "1", "username": "s",
                                                     "password": "x"}],
                                           "call": [{"name": "SAW", "mode": "answer", "accept_from": ["127.0.0.1"],
                                                     "codec": "mp2", "bitrate_kbps": 192, "channel_mode": "dual-mono",
                                                     "left": {"line": "S"}}]}, lookup_password=NOPW), None)
        await saw.start()
        saw_run = asyncio.ensure_future(saw.run())
        try:
            await manager.save("lines", None, line_form("Regie"))
            await manager.save("calls", None, profile_form("Studio", mode="both"))
            attach(service)
            attach(saw)
            r = service.runners[0]
            assert await wait_for(lambda: r.state == CallState.WAITING and saw.runners[0].state == CallState.WAITING)
            assert service.dial(r.uid, f"sip:x@127.0.0.1:{saw.ua.port}") is None
            assert await wait_for(lambda: r.state == CallState.CONNECTED)
            with pytest.raises(StoreError, match="Gerade im Gespräch: „Studio“"):
                await manager.save("calls", "Studio", profile_form("Studio", mode="both", bitrate_kbps="128"))
            with pytest.raises(StoreError, match="Gerade im Gespräch"):
                await manager.delete("calls", "Studio")
            assert r.state == CallState.CONNECTED and service.runners == [r]  # the call was not touched
            assert service.hangup(r.uid) is None
            assert await wait_for(lambda: r.state == CallState.WAITING)
            await manager.delete("calls", "Studio")
            assert service.runners == []
        finally:
            service.stop()
            saw.stop()
            await asyncio.wait_for(asyncio.gather(run, saw_run), 15)
            await service.close()
            await saw.close()

    asyncio.run(go())


def test_accounts_register_when_added_and_are_removed_again_when_deleted_or_changed(tmp_path):
    async def go():
        loop = asyncio.get_running_loop()
        reg = FakeRegistrar("4711", "secret")
        transport, _ = await loop.create_datagram_endpoint(lambda: reg, local_addr=("127.0.0.1", 0))
        port = transport.get_extra_info("sockname")[1]
        service, manager, store = await make(tmp_path)
        run = asyncio.ensure_future(service.run())
        form = {"name": "ARD", "registrar": f"sip:127.0.0.1:{port}", "user": "4711", "password": "secret",
                "domain": "sip.local"}
        try:
            await manager.save("accounts", None, form)
            assert await wait_for(lambda: service.registrations["ARD"].state == "registered")
            assert reg.bindings.get("4711") == 120
            # a change of the password re-registers (the old binding is removed first)
            await manager.save("accounts", "ARD", {**form, "password": "wrong"})
            assert await wait_for(lambda: service.registrations["ARD"].state == "failed")
            await manager.save("accounts", "ARD", {**form, "password": ""})  # empty = keep the stored (wrong) one
            await asyncio.sleep(0.3)
            assert store.get("accounts", "ARD")["password"] == "wrong"
            await manager.save("accounts", "ARD", form)
            assert await wait_for(lambda: service.registrations["ARD"].state == "registered")
            await manager.delete("accounts", "ARD")
            assert "ARD" not in service.registrations and reg.bindings == {}  # unregistered on the way out
            assert (await manager.test_account({**form}))["ok"] is True  # the test works without saving
            assert reg.bindings == {}
            bad = await manager.test_account({**form, "password": "nope"})
            assert bad["ok"] is False and "403" in bad["message"]
        finally:
            service.stop()
            await asyncio.wait_for(run, 15)
            await service.close()
            transport.close()

    asyncio.run(go())


def test_entries_of_the_toml_file_are_shown_but_cannot_be_touched(tmp_path):
    async def go():
        toml = {**BASE, "line": [{"name": "Fest", "production_id": "1", "line_id": "1", "username": "f", "password": "x"}],
                "call": [{"name": "Datei", "mode": "answer", "accept_from": ["127.0.0.1"], "codec": "g722",
                          "left": {"line": "Fest"}}]}
        service, manager, store = await make(tmp_path, toml)
        try:
            view = manager.view()
            assert [(c["name"], c["origin"]) for c in view["calls"]] == [("Datei", "file")]
            assert view["lines"][0]["origin"] == "file" and "password" not in view["lines"][0]
            with pytest.raises(StoreError, match="Konfigurationsdatei"):
                await manager.save("calls", "Datei", profile_form("Datei"))
            with pytest.raises(StoreError, match="Konfigurationsdatei"):
                await manager.delete("lines", "Fest")
            with pytest.raises(StoreError, match="gibt es schon"):
                await manager.save("calls", None, profile_form("Datei", left={"send": "Fest"}))
        finally:
            await service.close()

    asyncio.run(go())


# ---------------------------------------------------------------- the page's API

def test_the_api_creates_edits_deletes_and_never_returns_a_password(tmp_path):
    async def go():
        service, manager, store = await make(tmp_path)
        run = asyncio.ensure_future(service.run())
        web = WebServer(service, WebCfg(True, "127.0.0.1", 0), manager)
        await web.start()
        loop = asyncio.get_running_loop()

        def call(method, path, body=None, headers=None):
            raw = json.dumps(body).encode() if body is not None else b""
            h = {"X-Requested-With": "intercom-sip", "Content-Type": "application/json", **(headers or {})}
            return loop.run_in_executor(None, lambda: http(web.port, method, path, h, raw))

        try:
            code, _ = await call("POST", "/api/lines", line_form("Regie", password="geheim"))
            assert code == 200
            code, _ = await call("POST", "/api/calls", profile_form("Studio"))
            assert code == 200
            code, body = await call("GET", "/api/config")
            view = json.loads(body)
            assert code == 200 and "geheim" not in body.decode()
            assert view["lines"][0]["has_password"] is True and view["lines"][0]["used_by"] == ["Studio"]
            assert view["calls"][0]["left"] == {"send": "Regie", "receive": "Regie"} and view["server_url"]
            code, body = await call("PUT", "/api/calls/Studio", profile_form("Studio", bitrate_kbps="160"))
            assert code == 200 and service.runners[0].cfg.fmt.bitrate_kbps == 160
            code, body = await call("PUT", "/api/calls/Studio", profile_form("Studio", bitrate_kbps="161"))
            assert code == 400 and "161 kbit" in json.loads(body)["error"]
            code, body = await call("DELETE", "/api/lines/Regie")
            assert code == 400 and "benutzt" in json.loads(body)["error"]
            assert (await call("DELETE", "/api/calls/Studio"))[0] == 200
            assert (await call("DELETE", "/api/lines/Regie"))[0] == 200
            assert (await call("DELETE", "/api/lines/Regie"))[0] == 400
            # protections: no header, no change
            assert (await call("POST", "/api/lines", line_form("X"), {"X-Requested-With": ""}))[0] == 403
            assert (await call("GET", "/api/nothing"))[0] == 404
            assert store.names("lines") == [] and store.names("calls") == []
        finally:
            await web.close()
            service.stop()
            await asyncio.wait_for(run, 15)
            await service.close()

    asyncio.run(go())


def test_discovering_lines_reports_what_the_manager_says(tmp_path, monkeypatch):
    from gateway.lines import AvailableLine, LinesError

    async def go():
        service, manager, store = await make(tmp_path)
        seen = []

        async def fake(url, user, password):
            seen.append((url, user, password))
            if password == "bad":
                raise LinesError("Die Leitungsliste konnte nicht geladen werden (HTTP 500).")
            return [AvailableLine("3", "Sendung", "7", "Regie"), AvailableLine("3", "Sendung", "8", "Technik")]

        monkeypatch.setattr("intercom_sip.admin.fetch_available_lines", fake)
        try:
            found = await manager.discover_lines(" gw1 ", "pw")
            assert found[1] == {"production_id": "3", "production_name": "Sendung", "line_id": "8", "line_name": "Technik"}
            assert seen == [("http://127.0.0.1:1", "gw1", "pw")]
            with pytest.raises(StoreError, match="HTTP 500"):
                await manager.discover_lines("gw1", "bad")
            with pytest.raises(StoreError, match="Benutzername"):
                await manager.discover_lines("", "")
        finally:
            await service.close()

    asyncio.run(go())
