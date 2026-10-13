"""Reading and writing the gateway TOML file for the UI. Python can read TOML but not write it, so this
is a small writer for exactly the shapes the config uses: scalars, lists of scalars, tables and arrays of
tables. Comments in a hand-edited file are not preserved (the UI keeps a .bak of the previous version)."""
from __future__ import annotations

import json
import os
import shutil
import tomllib
from pathlib import Path
from typing import Any


def _scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)  # JSON string escapes are valid TOML basic-string escapes
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_scalar(x) for x in v) + "]"
    raise TypeError(f"cannot write {type(v).__name__} to TOML")


def _emit_table(header: str, table: dict, out: list[str]) -> None:
    out.append(header)
    for k, v in table.items():
        if v is None:
            continue
        out.append(f"{k} = {_scalar(v)}")
    out.append("")


def dumps(data: dict) -> str:
    out: list[str] = []
    for key, value in data.items():
        if isinstance(value, dict):
            _emit_table(f"[{key}]", value, out)
        elif isinstance(value, list) and value and all(isinstance(x, dict) for x in value):
            for item in value:
                _emit_table(f"[[{key}]]", item, out)
        else:
            raise TypeError(f"top-level key {key!r} must be a table or an array of tables")
    return "\n".join(out)


def load_raw(path: Path) -> dict:
    """The file as a dict; an empty skeleton when it does not exist yet."""
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {"server": {"url": ""}, "audio": {"backend": "portaudio", "playout_ms": 60}, "line": []}


def save(path: Path, data: dict) -> None:
    """Write atomically and keep the previous version as <name>.bak."""
    text = dumps(data)
    tomllib.loads(text)  # never write something we could not read back
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
