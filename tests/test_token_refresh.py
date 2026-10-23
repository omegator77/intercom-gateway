"""A revoked login (the account was logged out somewhere else) is replaced by a new one instead of leaving the line
with a token the server refuses: its people list and the clean closing of its session depend on it."""
import asyncio
from types import SimpleNamespace

import numpy as np

from aiortc.mediastreams import MediaStreamTrack
from gateway.auth import AuthClient
from gateway.config import AudioCfg, LineCfg, ServerCfg
from gateway.rtc import LineConnection
from gateway.session import LineWorker


class Track(MediaStreamTrack):
    kind = "audio"

    async def recv(self):
        raise NotImplementedError


class FakeHttp:
    """Accepts only the token in `valid`; records every request."""

    def __init__(self, valid):
        self.valid = valid
        self.requests = []

    async def request(self, method, url, headers=None, **kw):
        token = (headers or {}).get("Cookie", "").partition("=")[2]
        self.requests.append((method, url, token))
        ok = token in self.valid
        body = {"participants": [{"name": "Ben", "sessionId": "s2", "isActive": True}]}
        return SimpleNamespace(status_code=200 if ok else 401, json=lambda: body)

    async def delete(self, url, headers=None):
        return await self.request("DELETE", url, headers)


def connection(http, refresh):
    conn = LineConnection(http, "OLD", Track(), lambda pcm, key: None, refresh_token=refresh)
    conn.session_id = "s1"
    return conn


def test_a_refused_request_logs_in_again_once_and_uses_the_new_token_from_then_on():
    async def go():
        http = FakeHttp(valid={"NEW"})
        logins = []

        async def refresh(stale):
            logins.append(stale)
            return "NEW"

        conn = connection(http, refresh)
        try:
            people = await conn.participants("9", "3")
            assert [p["name"] for p in people] == ["Ben"] and logins == ["OLD"]
            assert [t for *_, t in http.requests] == ["OLD", "NEW"]
            await conn.heartbeat()
            await conn.close()
            assert [t for *_, t in http.requests][2:] == ["NEW", "NEW"]  # no second login for the following requests
            assert logins == ["OLD"]
        finally:
            await conn.pc.close()

    asyncio.run(go())


def test_a_valid_login_is_left_alone_and_a_failing_new_login_leaves_the_refusal_visible():
    async def go():
        http = FakeHttp(valid={"OLD"})
        calls = []

        async def refresh(stale):
            calls.append(stale)
            return "NEW"

        conn = connection(http, refresh)
        try:
            await conn.participants("9", "3")
            assert calls == []  # nothing was refused: no login
        finally:
            await conn.pc.close()

        http2 = FakeHttp(valid=set())

        async def broken(stale):
            raise RuntimeError("login rejected")

        conn2 = connection(http2, broken)
        try:
            from gateway.rtc import SessionError
            try:
                await conn2.participants("9", "3")
                raise AssertionError("the 401 must surface")
            except SessionError as e:
                assert e.status == 401
        finally:
            await conn2.pc.close()

    asyncio.run(go())


def test_without_a_refresh_function_a_refusal_is_just_reported():
    async def go():
        http = FakeHttp(valid=set())
        conn = LineConnection(http, "OLD", Track(), lambda pcm, key: None)
        conn.session_id = "s1"
        try:
            assert await conn.heartbeat() == 401 and len(http.requests) == 1
        finally:
            await conn.pc.close()

    asyncio.run(go())


def test_lines_that_notice_the_revoked_login_together_cause_one_new_login():
    async def go():
        logins = []

        class Http:
            async def post(self, url, json=None):
                logins.append(json["username"])
                await asyncio.sleep(0.05)
                return SimpleNamespace(status_code=200, headers=SimpleNamespace(
                    get_list=lambda name: [f"auth_token=T{len(logins)}; Path=/"]))

        auth = AuthClient(Http(), "anna", "pw")
        first = await auth.token()
        assert first == "T1"
        workers = [LineWorker(LineCfg("L%d" % i, "9", str(i), "anna", "pw"), ServerCfg("http://x"), AudioCfg("synthetic"),
                              lambda *a: None, None, auth=auth) for i in range(4)]
        tokens = await asyncio.gather(*[w._fresh_token(first) for w in workers])
        assert tokens == ["T2"] * 4 and len(logins) == 2  # the first login, and one more for all four lines
        # a late one that still holds the old token gets the current one, not yet another login
        assert await workers[0]._fresh_token(first) == "T2" and len(logins) == 2

    asyncio.run(go())
