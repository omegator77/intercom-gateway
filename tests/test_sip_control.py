"""Calling and hanging up on request (what the status page's buttons do), and the status page's own server."""
import asyncio
import base64
import json

import pytest

from intercom_sip.calls import CallState, SipService
from intercom_sip.config import ConfigError, WebCfg, parse_sip_config
from intercom_sip.web import WebServer
from test_sip_calls import PW, attach, base_cfg, config, wait_for


async def start_both_pair(me_extra=None, saw_mode="answer"):
    """Us in mode 'both' (answers, calls when asked) against SAW, a plain answering profile."""
    saw = SipService(config("answer", 0, 41200, {
        "name": "SAW", "mode": saw_mode, "accept_from": ["127.0.0.1"], "codec": "mp2", "bitrate_kbps": 192,
        "channel_mode": "dual-mono", "left": {"line": "SAW-L"}}, ["SAW-L"]), None)
    await saw.start()
    data = {"name": "Studio", "mode": "both", "accept_from": ["127.0.0.1"], "codec": "mp2", "bitrate_kbps": 192,
            "channel_mode": "dual-mono", "left": {"line": "Regie"}}
    data.update(me_extra or {})
    me = SipService(config("both", 0, 41000, data, ["Regie"]), None)
    await me.start()
    attach(saw)
    attach(me)
    return me, saw


async def finish(run, *services):
    for s in services:
        s.stop()
    await asyncio.wait_for(run, 10)
    for s in services:
        await s.close()


def test_a_both_profile_waits_for_calls_and_calls_only_when_asked_and_hangs_up_on_request():
    async def go():
        me, saw = await start_both_pair()
        run = asyncio.gather(me.run(), saw.run())
        mine, theirs = me.runners[0], saw.runners[0]
        try:
            assert await wait_for(lambda: mine.state == CallState.WAITING and theirs.state == CallState.WAITING)
            await asyncio.sleep(1.5)
            assert mine.state == CallState.WAITING and mine.calls_made == 0  # nothing is dialled by itself
            assert me.dial(0, None) == "keine Rufnummer angegeben"
            assert me.dial(0, f"sip:saw@127.0.0.1:{saw.ua.port}") is None
            assert await wait_for(lambda: mine.state == CallState.CONNECTED and theirs.state == CallState.CONNECTED)
            st = me.status()["calls"][0]
            assert st["direction"] == "out" and st["peer"].startswith("sip:") and st["can_hangup"] and not st["can_dial"]
            assert me.dial(0, "x") == "es läuft schon ein Anruf"
            assert me.hangup(0) is None
            assert await wait_for(lambda: mine.state == CallState.WAITING and theirs.state == CallState.WAITING)
            assert me.hangup(0) == "es läuft kein Anruf"
            assert saw.dial(0, "x") == "dieses Profil nimmt nur Anrufe an"
        finally:
            await finish(run, me, saw)

    asyncio.run(go())


def test_calling_the_both_profile_from_outside_works_and_shows_the_direction():
    async def go():
        me, saw = await start_both_pair({"redial": False}, saw_mode="both")
        run = asyncio.gather(me.run(), saw.run())
        mine, theirs = me.runners[0], saw.runners[0]
        try:
            assert await wait_for(lambda: theirs.state == CallState.WAITING and mine.state == CallState.WAITING)
            assert saw.dial(0, f"sip:studio@127.0.0.1:{me.ua.port}") is None
            assert await wait_for(lambda: mine.state == CallState.CONNECTED)
            assert me.status()["calls"][0]["direction"] == "in" and saw.status()["calls"][0]["direction"] == "out"
            assert saw.hangup(0) is None
            assert await wait_for(lambda: mine.state == CallState.WAITING)
        finally:
            await finish(run, me, saw)

    asyncio.run(go())


def test_hanging_up_a_dialling_profile_stops_it_calling_again_until_it_is_asked():
    async def go():
        saw = SipService(config("answer", 0, 41200, {
            "name": "SAW", "mode": "answer", "accept_from": ["127.0.0.1"], "codec": "mp2", "bitrate_kbps": 192,
            "channel_mode": "dual-mono", "left": {"line": "SAW-L"}}, ["SAW-L"]), None)
        await saw.start()
        target = f"sip:saw@127.0.0.1:{saw.ua.port}"
        me = SipService(config("dial", 0, 41000, {
            "name": "SAW", "mode": "dial", "target": target, "codec": "mp2", "bitrate_kbps": 192,
            "channel_mode": "dual-mono", "redial_s": 1, "left": {"line": "Regie"}}, ["Regie"]), None)
        await me.start()
        attach(saw)
        attach(me)
        run = asyncio.gather(me.run(), saw.run())
        mine, theirs = me.runners[0], saw.runners[0]
        try:
            assert await wait_for(lambda: mine.state == CallState.CONNECTED)
            assert me.hangup(0) is None
            assert await wait_for(lambda: mine.state == CallState.IDLE and theirs.state == CallState.WAITING)
            assert me.status()["calls"][0]["paused"]
            await asyncio.sleep(2.5)
            assert mine.state == CallState.IDLE and mine.calls_made == 1  # no calling again by itself
            assert me.dial(0, None) is None  # the button: back to calling the configured target
            assert await wait_for(lambda: mine.state == CallState.CONNECTED and mine.calls_made == 2)
            assert me.hangup(0) is None
            assert await wait_for(lambda: mine.state == CallState.IDLE)
            # a number typed in by hand is a single call, and the profile stays paused afterwards
            assert me.dial(0, target) is None
            assert await wait_for(lambda: mine.state == CallState.CONNECTED and mine.calls_made == 3)
            assert me.hangup(0) is None
            assert await wait_for(lambda: mine.state == CallState.IDLE)
            await asyncio.sleep(2.0)
            assert mine.state == CallState.IDLE and mine.calls_made == 3
        finally:
            await finish(run, me, saw)

    asyncio.run(go())


def test_autostart_false_keeps_a_dialling_profile_quiet_until_asked():
    async def go():
        saw = SipService(config("answer", 0, 41200, {
            "name": "SAW", "mode": "answer", "accept_from": ["127.0.0.1"], "codec": "g722",
            "left": {"line": "SAW-L"}}, ["SAW-L"]), None)
        await saw.start()
        me = SipService(config("dial", 0, 41000, {
            "name": "SAW", "mode": "dial", "autostart": False, "target": f"sip:saw@127.0.0.1:{saw.ua.port}",
            "codec": "g722", "left": {"line": "Regie"}}, ["Regie"]), None)
        await me.start()
        attach(saw)
        attach(me)
        run = asyncio.gather(me.run(), saw.run())
        mine = me.runners[0]
        try:
            await asyncio.sleep(2.0)
            assert mine.state == CallState.IDLE and mine.calls_made == 0
            assert me.dial(0, None) is None
            assert await wait_for(lambda: mine.state == CallState.CONNECTED)
        finally:
            await finish(run, me, saw)

    asyncio.run(go())


# ---------------------------------------------------------------- the status page

def http(port, method, path, headers=None, body=b""):
    """A tiny blocking HTTP client (it runs in a thread while the server under test lives on the test's event loop)."""
    import socket
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    h = {"Host": f"127.0.0.1:{port}", "Content-Length": str(len(body)), **(headers or {})}
    s.sendall((f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n").encode() + body)
    data = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    head, _, payload = data.partition(b"\r\n\r\n")
    http.last_headers = head.decode("latin-1")
    return int(head.split()[1]), payload


def test_the_status_page_serves_status_and_obeys_dial_and_hangup_with_its_protections():
    async def go():
        me, saw = await start_both_pair()
        run = asyncio.gather(me.run(), saw.run())
        web = WebServer(me, WebCfg(True, "127.0.0.1", 0))
        await web.start()
        port = web.port
        loop = asyncio.get_running_loop()

        def call(*a, **k):
            return loop.run_in_executor(None, lambda: http(port, *a, **k))

        post = {"X-Requested-With": "intercom-sip"}
        try:
            assert await wait_for(lambda: me.runners[0].state == CallState.WAITING)
            code, page = await call("GET", "/")
            assert code == 200 and b"SIP-Br" in page and b"/api/status" in page
            code, body = await call("GET", "/api/status")
            st = json.loads(body)
            assert code == 200 and st["calls"][0]["state"] == "waiting" and st["calls"][0]["id"] == 0 and st["version"]
            assert (await call("GET", "/nope"))[0] == 404
            assert (await call("GET", "/api/status", {"Host": "evil.example"}))[0] == 403  # DNS rebinding
            number = json.dumps({"number": f"sip:saw@127.0.0.1:{saw.ua.port}"}).encode()
            assert (await call("POST", "/api/calls/0/dial", {}, number))[0] == 403  # a foreign page cannot press the button
            assert (await call("POST", "/api/calls/0/dial", post, number))[0] == 200
            assert await wait_for(lambda: me.runners[0].state == CallState.CONNECTED)
            code, body = await call("POST", "/api/calls/0/dial", post, number)
            assert code == 409 and "Anruf" in json.loads(body)["error"]
            assert (await call("POST", "/api/calls/7/hangup", post))[0] == 404
            assert (await call("POST", "/api/calls/0/hangup", post))[0] == 200
            assert await wait_for(lambda: me.runners[0].state == CallState.WAITING)
            assert (await call("POST", "/api/calls/0/dial", post, b"{not json"))[0] == 400
        finally:
            await web.close()
            await finish(run, me, saw)

    asyncio.run(go())


def test_a_page_open_to_the_network_wants_a_password():
    data = base_cfg()
    data["web"] = {"listen": "0.0.0.0:8088"}
    with pytest.raises(ConfigError, match="needs a password"):
        parse_sip_config(data, lookup_password=PW)
    data["web"] = {"listen": "0.0.0.0:8088", "password": "pw"}
    assert parse_sip_config(data, lookup_password=PW).web.password == "pw"
    assert parse_sip_config(base_cfg(), lookup_password=PW).web.local_only
    with pytest.raises(ConfigError, match="listen must look like"):
        parse_sip_config({**base_cfg(), "web": {"listen": "nonsense"}}, lookup_password=PW)

    async def go():
        me, saw = await start_both_pair()
        # as if it listened on the network (no host check, password required); the socket itself stays on loopback
        web = WebServer(me, WebCfg(True, "192.0.2.77", 8088, "admin", "s3cret"))
        server = await asyncio.start_server(web._handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        loop = asyncio.get_running_loop()

        def call(*a, **k):
            return loop.run_in_executor(None, lambda: http(port, *a, **k))

        try:
            assert (await call("GET", "/api/status"))[0] == 401
            bad = {"Authorization": "Basic " + base64.b64encode(b"admin:wrong").decode()}
            good = {"Authorization": "Basic " + base64.b64encode(b"admin:s3cret").decode()}
            assert (await call("GET", "/api/status", bad))[0] == 401
            assert (await call("GET", "/api/status", good))[0] == 200
            # the page itself hands out a cookie, and that is enough for its own requests afterwards
            assert (await call("GET", "/", good))[0] == 200
            cookie = [l for l in http.last_headers.split("\r\n") if l.lower().startswith("set-cookie")][0]
            value = cookie.split(":", 1)[1].split(";")[0].strip()
            assert (await call("GET", "/api/status", {"Cookie": value}))[0] == 200
            assert (await call("GET", "/api/status", {"Cookie": "sipui=wrong"}))[0] == 401
        finally:
            server.close()
            await server.wait_closed()
            await me.close()
            await saw.close()

    asyncio.run(go())
