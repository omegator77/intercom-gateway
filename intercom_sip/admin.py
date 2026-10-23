"""Changing accounts, intercom lines and call profiles while the bridge runs - what the settings part of the status page
talks to. Every change is validated by building the whole configuration, applied to the running service, and only then
written to the settings file; if any step fails nothing has changed."""
from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Callable, Mapping

from gateway.auth import LoginError
from gateway.lines import LinesError, fetch_available_lines

from .calls import ReconfigError, SipService
from .config import ConfigError, SipConfig, merge_raw, parse_sip_config
from .registration import RegisterCfg, Registration
from .sip import SipError
from .store import KINDS, LABEL, SECRET, TOML_KEY, Store, StoreError, used_by

log = logging.getLogger("sip.admin")

# what the page may set, and how each value is read
FIELDS: dict[str, dict[str, type]] = {
    "accounts": {"name": str, "registrar": str, "user": str, "domain": str, "auth_user": str, "contact_user": str,
                 "expires_s": int, "password": str},
    "lines": {"name": str, "production_id": str, "line_id": str, "username": str, "password": str},
    "calls": {"name": str, "mode": str, "account": str, "target": str, "codec": str, "channel_mode": str,
              "sample_rate": int, "bitrate_kbps": int, "ptime_ms": int, "playout_ms": int, "redial_s": float,
              "rtp_timeout_s": float, "in_gain_db": float, "out_gain_db": float, "autostart": bool, "redial": bool,
              "keep_lines": bool, "rtp_latch": bool, "accept_from": list},
}
REQUIRED = {"accounts": ("name", "registrar", "user"), "lines": ("name", "production_id", "line_id", "username"),
            "calls": ("name",)}
PRETTY = {"name": "Name", "registrar": "Server", "user": "Nummer", "production_id": "Produktion", "line_id": "Leitung",
          "username": "Benutzername"}


def _coerce(key: str, value, typ: type):
    if typ is bool:
        return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "ja", "on")
    if typ is list:
        items = value if isinstance(value, list) else str(value).replace(";", ",").split(",")
        return [str(i).strip() for i in items if str(i).strip()]
    try:
        return typ(str(value).strip().replace(",", ".")) if typ is float else typ(str(value).strip())
    except ValueError:
        raise StoreError(f"Ungültiger Wert für „{PRETTY.get(key, key)}“: {value!r}") from None


def clean(kind: str, payload: Mapping, existing: Mapping | None) -> dict:
    """The page's form -> a stored entry: only known fields, numbers as numbers, empty = not set, and an empty
    password keeps the one that is stored."""
    entry: dict = {}
    for key, typ in FIELDS[kind].items():
        if key not in payload:
            continue
        value = payload[key]
        if value is None or (isinstance(value, str) and not value.strip()) or value == []:
            continue
        entry[key] = _coerce(key, value, typ)
    if kind == "calls":
        for side in ("left", "right"):
            ch = payload.get(side) or {}
            sub = {k: str(ch[k]).strip() for k in ("send", "receive") if str(ch.get(k) or "").strip()}
            if sub:
                entry[side] = sub
    secret = SECRET[kind]
    if secret in FIELDS[kind] and secret not in entry and existing and existing.get(secret):
        entry[secret] = existing[secret]
    for key in REQUIRED[kind]:
        if key not in entry:
            raise StoreError(f"Bitte „{PRETTY.get(key, key)}“ ausfüllen.")
    if kind == "lines" and "password" not in entry:
        raise StoreError("Bitte das Passwort des Intercom-Accounts eintragen.")
    return entry


class ConfigManager:
    def __init__(self, service: SipService, toml_data: Mapping, store: Store, env: Mapping[str, str] | None = None,
                 lookup_password: Callable[[str, str], str | None] | None = None):
        self.service = service
        self.lookup = lookup_password
        self.toml = dict(toml_data)
        self.store = store
        self.env = env
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ building
    def build(self) -> SipConfig:
        return parse_sip_config(merge_raw(self.toml, self.store.raw()), self.env, self.lookup, require_calls=False)

    def merged_raw(self) -> dict:
        return merge_raw(self.toml, self.store.raw())

    # ------------------------------------------------------------ what the page shows
    def view(self) -> dict:
        raw = self.merged_raw()
        out: dict = {"server_url": self.service.cfg.server.url, "codecs": {}}
        for kind in KINDS:
            items = []
            file_names = {e["name"] for e in self.toml.get(TOML_KEY[kind], [])}
            for e in raw.get(TOML_KEY[kind], []):
                item = {k: v for k, v in e.items() if k not in (SECRET[kind], "password", "auth_password")}
                item["has_password"] = bool(e.get("password") or e.get("password_env") or e.get("auth_password"))
                item["origin"] = "file" if e["name"] in file_names else "page"
                if kind != "calls":
                    item["used_by"] = used_by(raw, kind, e["name"])
                items.append(item)
            out[kind] = items
        return out

    # ------------------------------------------------------------ changing
    async def save(self, kind: str, name: str | None, payload: Mapping) -> None:
        """Create (`name` is None) or change the entry called `name`."""
        async with self._lock:
            file_names = {e["name"] for e in self.toml.get(TOML_KEY[kind], [])}
            if name in file_names:
                raise StoreError(f"{LABEL[kind]} „{name}“ steht in der Konfigurationsdatei und lässt sich hier nicht ändern.")
            existing = self.store.get(kind, name) if name else None
            if name and existing is None:
                raise StoreError(f"{LABEL[kind]} „{name}“ gibt es nicht.")
            entry = clean(kind, payload, existing)
            if entry["name"] in file_names:
                raise StoreError(f"{LABEL[kind]} „{entry['name']}“ gibt es schon (in der Konfigurationsdatei).")
            await self._commit(lambda: self.store.put(kind, entry, name))

    async def delete(self, kind: str, name: str) -> None:
        async with self._lock:
            if name in {e["name"] for e in self.toml.get(TOML_KEY[kind], [])}:
                raise StoreError(f"{LABEL[kind]} „{name}“ steht in der Konfigurationsdatei und lässt sich hier nicht löschen.")
            users = used_by(self.merged_raw(), kind, name)
            if users:
                raise StoreError(f"{LABEL[kind]} „{name}“ wird noch benutzt von: " + ", ".join(f"„{u}“" for u in users) + ".")
            await self._commit(lambda: self.store.delete(kind, name))

    async def _commit(self, change: Callable[[], None]) -> None:
        snap = self.store.snapshot()
        try:
            change()
            try:
                new = self.build()
            except ConfigError as e:
                raise StoreError(f"Ungültig: {e}") from e
            try:
                await self.service.apply(new)
            except ReconfigError as e:
                raise StoreError(str(e)) from e
            self.store.save()
        except BaseException:
            self.store.restore(snap)
            raise

    # ------------------------------------------------------------ helpers of the page
    async def discover_lines(self, username: str, password: str, name: str | None = None) -> list[dict]:
        """The intercom lines this account may join: [{production_id, production_name, line_id, line_name}].
        With the `name` of a stored line and no password typed in, that line's own login is used."""
        stored = self.store.get("lines", name) if name else None
        if stored and not password:
            username, password = username or stored.get("username", ""), stored.get("password", "")
        username = username.strip()
        if not username or not password:
            raise StoreError("Bitte Benutzername und Passwort des Intercom-Accounts eintragen.")
        try:
            found = await fetch_available_lines(self.service.cfg.server.url, username, password)
        except (LoginError, LinesError) as e:
            raise StoreError(str(e)) from e
        return [{"production_id": f.production_id, "production_name": f.production_name, "line_id": f.line_id,
                 "line_name": f.line_name} for f in found]

    async def test_account(self, payload: Mapping) -> dict:
        """Try a registration with the form's values (with a contact of its own, so a running registration of the same
        account is not disturbed), or just report the state of an unchanged running one."""
        name = str(payload.get("name") or "").strip()
        existing = self.store.get("accounts", name) if name else None
        entry = clean("accounts", payload, existing)
        cfg = parse_sip_config(merge_raw({**self.toml, "line": [], "call": []}, {"account": [entry]}), self.env,
                               self.lookup, require_calls=False).accounts[entry["name"]]
        running = self.service.registrations.get(entry["name"])
        if running is not None and running.cfg == cfg:
            state = running.state
            return {"ok": state == "registered", "message": {
                "registered": "Angemeldet.", "failed": f"Anmeldung fehlgeschlagen: {running.last_error}",
                "idle": "Meldet gerade an …"}.get(state, state)}
        probe = Registration(self.service.ua, RegisterCfg(
            registrar=cfg.registrar, user=cfg.user, password=cfg.password, domain=cfg.domain, auth_user=cfg.auth_user,
            expires_s=60, name=cfg.name, contact_user=f"test-{secrets.token_hex(3)}"))
        try:
            await probe.register(60)
            await probe.register(0)
        except (SipError, OSError) as e:
            return {"ok": False, "message": f"Anmeldung fehlgeschlagen: {e}"}
        return {"ok": True, "message": "Anmeldung klappt (und wurde wieder entfernt)."}
