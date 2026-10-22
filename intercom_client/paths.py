"""Where the client keeps its per-user state (config, log)."""
from __future__ import annotations

import os
from pathlib import Path


def state_dir(override: str | Path | None = None) -> Path:
    """`override` > $INTERCOM_CLIENT_HOME > %LOCALAPPDATA%\\intercom-client > ~/.intercom-client. Not created here."""
    if override:
        return Path(override).expanduser()
    if os.environ.get("INTERCOM_CLIENT_HOME"):
        return Path(os.environ["INTERCOM_CLIENT_HOME"]).expanduser()
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "intercom-client" if base else Path.home() / ".intercom-client"


def default_config(home: Path) -> Path:
    return home / "client.toml"


def default_log(home: Path) -> Path:
    return home / "client.log"
