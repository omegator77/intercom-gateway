"""What the status page manages: accounts, intercom lines and call profiles, kept in one JSON file.

The entries have the same shape as the `[[account]]`, `[[line]]` and `[[call]]` tables of the TOML file (so the same
validation reads them); the TOML file stays the place for the fixed things (server, SIP port, page) and may still
define entries of its own, which the page shows but does not change. Passwords are stored in the file in plain text,
so it is written for its owner only."""
from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path

KINDS = ("accounts", "lines", "calls")
TOML_KEY = {"accounts": "account", "lines": "line", "calls": "call"}
SECRET = {"accounts": "password", "lines": "password", "calls": "auth_password"}
LABEL = {"accounts": "Konto", "lines": "Leitung", "calls": "Anruf-Profil"}


class StoreError(Exception):
    """The message is fit to show the user."""


class Store:
    def __init__(self, path: Path | None):
        self.path = path
        self.data: dict[str, list[dict]] = {k: [] for k in KINDS}

    # ------------------------------------------------------------ file
    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise StoreError(f"Die Einstellungsdatei {self.path} ist nicht lesbar: {e}") from e
        for kind in KINDS:
            entries = raw.get(kind, [])
            if not isinstance(entries, list) or not all(isinstance(e, dict) and e.get("name") for e in entries):
                raise StoreError(f"Die Einstellungsdatei {self.path} ist beschädigt ({kind}).")
            self.data[kind] = entries

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": 1, **self.data}, f, indent=2, ensure_ascii=False)
            try:
                os.chmod(tmp, 0o600)  # passwords inside; has no effect on Windows
            except OSError:
                pass
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------ access
    def snapshot(self) -> dict:
        return copy.deepcopy(self.data)

    def restore(self, snap: dict) -> None:
        self.data = snap

    def names(self, kind: str) -> list[str]:
        return [e["name"] for e in self.data[kind]]

    def get(self, kind: str, name: str) -> dict | None:
        return next((e for e in self.data[kind] if e["name"] == name), None)

    def raw(self) -> dict[str, list[dict]]:
        """The entries as TOML-shaped tables, ready to be merged into the parsed file."""
        return {TOML_KEY[k]: copy.deepcopy(self.data[k]) for k in KINDS}

    # ------------------------------------------------------------ change
    def put(self, kind: str, entry: dict, old_name: str | None = None) -> None:
        """Add `entry`, or replace the one called `old_name` (a new name is carried into the entries that refer to it)."""
        name = entry["name"]
        if old_name is None:
            if self.get(kind, name) is not None:
                raise StoreError(f"{LABEL[kind]} „{name}“ gibt es schon.")
            self.data[kind].append(entry)
            return
        old = self.get(kind, old_name)
        if old is None:
            raise StoreError(f"{LABEL[kind]} „{old_name}“ gibt es nicht.")
        if name != old_name and self.get(kind, name) is not None:
            raise StoreError(f"{LABEL[kind]} „{name}“ gibt es schon.")
        self.data[kind][self.data[kind].index(old)] = entry
        if name != old_name:
            self._rename_references(kind, old_name, name)

    def delete(self, kind: str, name: str) -> None:
        old = self.get(kind, name)
        if old is None:
            raise StoreError(f"{LABEL[kind]} „{name}“ gibt es nicht.")
        self.data[kind].remove(old)

    def _rename_references(self, kind: str, old: str, new: str) -> None:
        for call in self.data["calls"]:
            if kind == "accounts" and call.get("account") == old:
                call["account"] = new
            if kind == "lines":
                for side in ("left", "right"):
                    ch = call.get(side) or {}
                    for key in ("line", "send", "receive"):
                        if ch.get(key) == old:
                            ch[key] = new


def used_by(raw: dict, kind: str, name: str) -> list[str]:
    """Names of the call profiles (in the merged TOML-shaped data) that use this account or line."""
    users = []
    for call in raw.get("call", []):
        if kind == "accounts" and call.get("account") == name:
            users.append(call["name"])
        if kind == "lines" and any(
                (call.get(side) or {}).get(key) == name for side in ("left", "right") for key in ("line", "send", "receive")):
            users.append(call["name"])
    return users
