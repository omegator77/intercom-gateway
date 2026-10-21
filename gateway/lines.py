"""Which lines an account may join: the productions the manager lists for it, with their lines.

The manager only returns the productions the account is a member of (every production for a super admin)."""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from .auth import API, AuthClient
from .http import create_client

PAGE = 50
MAX_PAGES = 100  # a runaway server answer must not loop forever


class LinesError(Exception):
    """The line list could not be fetched; the message is fit to show the user."""


@dataclass(frozen=True)
class AvailableLine:
    production_id: str
    production_name: str
    line_id: str
    line_name: str

    @property
    def label(self) -> str:
        return f"{self.production_name}  ›  {self.line_name}"


async def list_lines(http: httpx.AsyncClient, token: str) -> list[AvailableLine]:
    """Every line of every production visible to the token's account, productions sorted by name and each
    production's lines in the order the manager keeps them."""
    found: list[AvailableLine] = []
    offset = 0
    for _ in range(MAX_PAGES):
        try:
            r = await http.get(f"{API}/productionlist", params={"extended": "true", "limit": PAGE, "offset": offset},
                               headers={"Cookie": f"auth_token={token}"})
        except httpx.HTTPError as e:
            raise LinesError(f"Server nicht erreichbar: {e}") from e
        if r.status_code != 200:
            raise LinesError(f"Die Leitungsliste konnte nicht geladen werden (HTTP {r.status_code}).")
        try:
            data = r.json()
            productions = data["productions"]
            total = int(data["totalItems"])
            for p in productions:
                for line in p.get("lines") or []:
                    found.append(AvailableLine(str(p["productionId"]), str(p["name"]), str(line["id"]), str(line["name"])))
        except (ValueError, KeyError, TypeError) as e:
            raise LinesError("Unerwartete Antwort des Servers auf die Leitungsliste.") from e
        offset += PAGE  # the manager slices before it drops productions without lines, so count by page, not by result
        if offset >= total:
            break
    order = {}
    for ln in found:
        order.setdefault(ln.production_id, len(order))
    return sorted(found, key=lambda ln: (ln.production_name.casefold(), order[ln.production_id]))


async def fetch_available_lines(server_url: str, username: str, password: str) -> list[AvailableLine]:
    """Log in with the account and list its lines. Raises auth.LoginError or LinesError."""
    http = await create_client(server_url)
    try:
        token = await AuthClient(http, username, password).token()
        return await list_lines(http, token)
    finally:
        await http.aclose()
