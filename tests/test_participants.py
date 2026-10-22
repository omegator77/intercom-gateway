"""'Who else is on the line': parsing the server's answer, polling it without ever disturbing the session,
and how the main window shows it."""
import asyncio
from types import SimpleNamespace

import pytest

from gateway import session as session_mod
from gateway.config import AudioCfg, LineCfg, ServerCfg
from gateway.rtc import SessionError, parse_participants
from gateway.session import LineWorker


def person(name, sid="s-x", active=True, whip=False):
    return {"name": name, "sessionId": sid, "isActive": active, "isWhip": whip}


# ---------------------------------------------------------------- parsing

def test_parse_leaves_out_this_gateway_sorts_and_flags():
    data = {"participants": [person("zoe", "s3", active=False), person("Gateway", "me"),
                             person("anna", "s1", whip=True), person("Ben", "s2")]}
    assert parse_participants(data, own_session="me") == [
        {"name": "anna", "active": True, "whip": True},
        {"name": "Ben", "active": True, "whip": False},
        {"name": "zoe", "active": False, "whip": False},
    ]


def test_parse_tolerates_missing_or_odd_data():
    assert parse_participants({}) == [] and parse_participants(None) == []
    assert parse_participants({"participants": None}) == []
    assert parse_participants({"participants": ["x", 3, {"sessionId": "s"}, {"name": ""}, person("ok")]}) == [
        {"name": "ok", "active": True, "whip": False}]
    assert len(parse_participants({"participants": [person("a", "me")]}, own_session=None)) == 1


# ---------------------------------------------------------------- polling

class FakeConn:
    def __init__(self, results):
        self.results = list(results)  # each item: a list to return or an Exception to raise
        self.calls = 0

    async def participants(self, production_id, line_id):
        self.calls += 1
        r = self.results[min(self.calls - 1, len(self.results) - 1)]
        if isinstance(r, Exception):
            raise r
        return r


def make_worker():
    cfg = LineCfg(name="PGM", production_id="1", line_id="2", username="u", password="p")
    return LineWorker(cfg, ServerCfg("http://x"), AudioCfg("synthetic"), lambda *a: None, http=None)


async def run_poll(worker, conn, seconds, monkeypatch):
    monkeypatch.setattr(session_mod, "PARTICIPANTS_S", 0.02)
    task = asyncio.ensure_future(worker._poll_participants(conn))
    await asyncio.sleep(seconds)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_poll_publishes_the_list_and_keeps_following_changes(monkeypatch):
    async def go():
        w = make_worker()
        both = [{"name": "a", "active": True, "whip": False}, {"name": "b", "active": False, "whip": True}]
        await run_poll(w, FakeConn([[], both]), 0.15, monkeypatch)
        assert w.participants == both
    asyncio.run(go())


def test_poll_survives_errors_and_shows_unknown_only_after_repeated_failures(monkeypatch):
    async def go():
        w = make_worker()
        w.participants = [{"name": "old", "active": True, "whip": False}]
        conn = FakeConn([SessionError("boom", 500)])
        monkeypatch.setattr(session_mod, "PARTICIPANTS_S", 0.02)
        task = asyncio.ensure_future(w._poll_participants(conn))
        await asyncio.sleep(0.03)  # one or two failures: the last known list stays
        assert w.participants is not None and not task.done()
        await asyncio.sleep(0.15)  # now it is unknown, and the poller is still alive to recover
        assert w.participants is None and not task.done()
        conn.results = [[{"name": "back", "active": True, "whip": False}]]
        await asyncio.sleep(0.1)
        assert w.participants == [{"name": "back", "active": True, "whip": False}]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    asyncio.run(go())


def test_status_carries_the_list_and_none_when_unknown():
    w = make_worker()
    assert w.status()["participants"] is None
    w.participants = [{"name": "a", "active": True, "whip": False}]
    assert w.status()["participants"] == [{"name": "a", "active": True, "whip": False}]


# ---------------------------------------------------------------- display

def test_people_html():
    from gateway.ui.window import people_html

    assert people_html(None) == ""
    assert "niemand" in people_html([])
    html = people_html([{"name": "<b>Anna</b>", "active": True, "whip": False},
                        {"name": "Butt", "active": False, "whip": True}])
    assert "&lt;b&gt;Anna&lt;/b&gt;" in html and "<b>Anna</b>" not in html  # names come from other users: escaped
    assert "(inaktiv)" in html and "WHIP" in html
