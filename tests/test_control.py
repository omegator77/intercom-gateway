import asyncio
import json
import urllib.error
import urllib.request

import pytest

from gateway.control import ControlError, ControlServer, fetch_status, find_running, read_control, request_stop


def run(coro):
    return asyncio.run(coro)


async def with_server(tmp_path, body):
    stopped = []
    srv = ControlServer(tmp_path, lambda: {"lines": [{"line": "a"}]}, lambda: stopped.append(1), "cfg.toml")
    await srv.start()
    try:
        await body(srv, stopped)
    finally:
        await srv.close()


def test_status_and_stop_roundtrip(tmp_path):
    async def body(srv, stopped):
        info = read_control(tmp_path)
        assert info["port"] == srv.port and info["config"] == "cfg.toml"
        assert (await asyncio.to_thread(fetch_status, info))["lines"] == [{"line": "a"}]
        assert (await asyncio.to_thread(find_running, tmp_path)) is not None
        await asyncio.to_thread(request_stop, info)
        await asyncio.sleep(0.05)
        assert stopped == [1]

    run(with_server(tmp_path, body))
    assert read_control(tmp_path) is None  # control file removed on close


def test_wrong_token_is_rejected_and_does_not_stop(tmp_path):
    async def body(srv, stopped):
        info = dict(read_control(tmp_path), token="nope")
        with pytest.raises(ControlError):
            await asyncio.to_thread(fetch_status, info)
        with pytest.raises(ControlError):
            await asyncio.to_thread(request_stop, info)
        await asyncio.sleep(0.05)
        assert stopped == []

    run(with_server(tmp_path, body))


def test_get_stop_does_not_stop_and_unknown_path_404(tmp_path):
    async def body(srv, stopped):
        info = read_control(tmp_path)

        def get(path, method="GET"):
            req = urllib.request.Request(f"http://127.0.0.1:{info['port']}{path}", method=method,
                                         headers={"X-Gateway-Token": info["token"]})
            try:
                return urllib.request.urlopen(req, timeout=2).status
            except urllib.error.HTTPError as e:
                return e.code

        assert await asyncio.to_thread(get, "/stop") == 404  # stopping needs POST
        assert await asyncio.to_thread(get, "/nothing") == 404
        assert stopped == []

    run(with_server(tmp_path, body))


def test_garbage_request_does_not_kill_server(tmp_path):
    async def body(srv, stopped):
        r, w = await asyncio.open_connection("127.0.0.1", srv.port)
        w.write(b"\x00\x01garbage\r\n\r\n")
        await w.drain()
        assert b"400" in (await r.read(200))
        w.close()
        assert (await asyncio.to_thread(find_running, tmp_path)) is not None

    run(with_server(tmp_path, body))


def test_leftover_control_file_means_not_running(tmp_path):
    (tmp_path / "control.json").write_text(json.dumps({"pid": 1, "port": 9, "token": "x"}))
    assert find_running(tmp_path) is None
    (tmp_path / "control.json").write_text("{not json")
    assert read_control(tmp_path) is None and find_running(tmp_path) is None


def test_close_keeps_a_newer_instances_file(tmp_path):
    async def body(srv, stopped):
        (tmp_path / "control.json").write_text(json.dumps({"pid": 2, "port": 1, "token": "other"}))

    run(with_server(tmp_path, body))
    assert read_control(tmp_path)["token"] == "other"
