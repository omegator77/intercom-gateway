"""Where the gateway keeps its per-user state (control file, log, default config)."""
from __future__ import annotations

import os
from pathlib import Path


def state_dir(override: str | Path | None = None) -> Path:
    """`override` > $INTERCOM_GATEWAY_HOME > %LOCALAPPDATA%\\intercom-gateway > ~/.intercom-gateway. Not created here."""
    if override:
        return Path(override).expanduser()
    if os.environ.get("INTERCOM_GATEWAY_HOME"):
        return Path(os.environ["INTERCOM_GATEWAY_HOME"]).expanduser()
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "intercom-gateway" if base else Path.home() / ".intercom-gateway"


def default_config(home: Path) -> Path:
    return home / "gateway.toml"


def default_log(home: Path) -> Path:
    return home / "gateway.log"
