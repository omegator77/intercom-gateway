"""One shared HTTP client for the whole gateway.

Constructing an httpx client builds an SSL context, which costs 0.6-0.9 s on this Windows setup and
blocks the event loop for that long - every login and every reconnect used to freeze the audio of all
other lines. The context is now built once, off the loop, and the client is reused.
"""
from __future__ import annotations

import asyncio
import http.cookiejar

import httpx


async def create_client(base_url: str) -> httpx.AsyncClient:
    ctx = await asyncio.to_thread(httpx.create_ssl_context)
    client = httpx.AsyncClient(base_url=base_url, verify=ctx, timeout=15)
    # Each line authenticates with its own account and sends its token explicitly per request. The
    # client must therefore never store or replay cookies itself, or lines would leak into each other.
    client.cookies.jar.set_policy(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
    return client
