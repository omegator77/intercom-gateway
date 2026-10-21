"""Listing the lines an account may join, against a fake manager (httpx.MockTransport)."""
import asyncio

import httpx
import pytest

from gateway import lines
from gateway.lines import AvailableLine, LinesError, list_lines


def run(coro):
    return asyncio.run(coro)


def prod(pid, name, line_names):
    return {"productionId": str(pid), "name": name,
            "lines": [{"id": str(i + 1), "name": n, "smbConferenceId": "x", "participants": []}
                      for i, n in enumerate(line_names)]}


def manager(pages, seen=None):
    """A fake /productionlist that serves `pages` (a list of production lists) by offset."""
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        q = request.url.params
        offset, limit = int(q["offset"]), int(q["limit"])
        index = offset // limit
        body = {"productions": pages[index] if index < len(pages) else [], "offset": offset, "limit": limit,
                "totalItems": sum(len(p) for p in pages)}
        return httpx.Response(200, json=body)
    return httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handler))


def test_lists_every_line_with_production_and_sends_the_token():
    seen = []

    async def go():
        async with manager([[prod(4, "Staplercup", ["Regie", "Ton", "PGM"]), prod(9, "Alpha", ["Kamera"])]], seen) as http:
            return await list_lines(http, "tok123")

    got = run(go())
    assert got == [AvailableLine("9", "Alpha", "1", "Kamera"),
                   AvailableLine("4", "Staplercup", "1", "Regie"),
                   AvailableLine("4", "Staplercup", "2", "Ton"),
                   AvailableLine("4", "Staplercup", "3", "PGM")]  # productions by name, lines as the server keeps them
    assert seen[0].headers["cookie"] == "auth_token=tok123"
    assert seen[0].url.params["extended"] == "true"
    assert got[1].label == "Staplercup  ›  Regie"


def test_follows_the_pages(monkeypatch):
    monkeypatch.setattr(lines, "PAGE", 2)
    pages = [[prod(1, "A", ["x"]), prod(2, "B", ["y"])], [prod(3, "C", ["z"])]]

    async def go():
        async with manager(pages) as http:
            return await list_lines(http, "t")

    assert [ln.production_name for ln in run(go())] == ["A", "B", "C"]


def test_account_without_productions_gives_an_empty_list():
    async def go():
        async with manager([[]]) as http:
            return await list_lines(http, "t")

    assert run(go()) == []


def test_skips_productions_without_lines():
    async def go():
        async with manager([[{"productionId": "1", "name": "Empty"}, prod(2, "B", ["y"])]]) as http:
            return await list_lines(http, "t")

    assert [ln.production_name for ln in run(go())] == ["B"]


@pytest.mark.parametrize("response", [
    httpx.Response(401, text="Login required"),
    httpx.Response(500, text="boom"),
])
def test_http_errors_become_a_readable_message(response):
    async def go():
        http = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(lambda r: response))
        async with http:
            return await list_lines(http, "t")

    with pytest.raises(LinesError, match="HTTP"):
        run(go())


def test_garbage_answer_is_reported():
    async def go():
        http = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"unexpected": True})))
        async with http:
            return await list_lines(http, "t")

    with pytest.raises(LinesError, match="Unerwartete"):
        run(go())


def test_unreachable_server_is_reported():
    def boom(request):
        raise httpx.ConnectError("refused")

    async def go():
        async with httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(boom)) as http:
            return await list_lines(http, "t")

    with pytest.raises(LinesError, match="nicht erreichbar"):
        run(go())
