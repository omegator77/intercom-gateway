"""Configuration: TOML file -> validated, immutable dataclasses."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from . import credentials


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class ServerCfg:
    url: str
    ice_servers: tuple[str, ...] = ()


@dataclass(frozen=True)
class AudioCfg:
    backend: str = "portaudio"  # "portaudio" or "synthetic" (test tones, no sound card)
    playout_ms: int = 60  # target jitter-buffer fill before playback starts


@dataclass(frozen=True)
class LineCfg:
    name: str
    production_id: str
    line_id: str
    username: str
    password: str = ""
    input_device: str | None = None  # capture, e.g. "DVS Receive 1-2" (audio coming FROM the Dante network)
    input_channels: tuple[int, ...] = (0,)
    in_gain_db: float = 0.0
    output_device: str | None = None  # playback, e.g. "DVS Transmit 1-2" (audio going TO the Dante network)
    output_channels: tuple[int, ...] = (0, 1)
    out_gain_db: float = 0.0
    tone_hz: float | None = None  # synthetic backend only: tone to send


@dataclass(frozen=True)
class Config:
    server: ServerCfg
    audio: AudioCfg
    lines: tuple[LineCfg, ...]


def _channels(value, field: str, line: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(c, int) and c >= 0 for c in value):
        raise ConfigError(f"line {line!r}: {field} must be a non-empty list of channel indexes >= 0")
    return tuple(value)


def _gain(value, field: str, line: str) -> float:
    try:
        g = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"line {line!r}: {field} must be a number (dB)") from None
    if not -40.0 <= g <= 40.0:
        raise ConfigError(f"line {line!r}: {field} must be between -40 and +40 dB")
    return g


def parse_config(data: Mapping, env: Mapping[str, str] | None = None,
                 lookup_password: Callable[[str, str], str | None] | None = None) -> Config:
    env = os.environ if env is None else env

    server = data.get("server") or {}
    url = str(server.get("url", "")).strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ConfigError("[server] url must start with http:// or https://")
    ice = server.get("ice_servers", [])
    if not isinstance(ice, list) or not all(isinstance(s, str) for s in ice):
        raise ConfigError("[server] ice_servers must be a list of strings")

    audio_raw = data.get("audio") or {}
    backend = str(audio_raw.get("backend", "portaudio"))
    if backend not in ("portaudio", "synthetic"):
        raise ConfigError("[audio] backend must be 'portaudio' or 'synthetic'")
    playout = int(audio_raw.get("playout_ms", 60))
    if not 20 <= playout <= 500:
        raise ConfigError("[audio] playout_ms must be between 20 and 500")

    lines: list[LineCfg] = []
    seen: set[str] = set()
    for raw in data.get("line", []):
        name = str(raw.get("name", "")).strip()
        if not name:
            raise ConfigError("every [[line]] needs a name")
        if name in seen:
            raise ConfigError(f"duplicate line name {name!r}")
        seen.add(name)
        if not raw.get("enabled", True):
            continue
        for key in ("production_id", "line_id", "username"):
            if raw.get(key) in (None, ""):
                raise ConfigError(f"line {name!r}: {key} is required")

        password = ""
        if raw.get("password_env"):
            var = str(raw["password_env"])
            if var not in env or not env[var]:
                raise ConfigError(f"line {name!r}: environment variable {var} is not set")
            password = env[var]
        elif raw.get("password"):
            password = str(raw["password"])
        else:
            password = (lookup_password or credentials.get_password)(url, str(raw["username"])) or ""
            if not password:
                raise ConfigError(f"line {name!r}: no password - enter it in the UI (stored in the Windows "
                                  "Credential Manager) or set password_env")

        relay = str(raw.get("relay_type", "mixed"))
        if relay != "mixed":
            raise ConfigError(f"line {name!r}: only relay_type 'mixed' is supported by the gateway")

        in_dev = raw.get("input_device") or None
        out_dev = raw.get("output_device") or None
        if backend == "portaudio" and not (in_dev or out_dev):
            raise ConfigError(f"line {name!r}: set input_device and/or output_device")

        lines.append(
            LineCfg(
                name=name,
                production_id=str(raw["production_id"]),
                line_id=str(raw["line_id"]),
                username=str(raw["username"]),
                password=password,
                input_device=in_dev,
                input_channels=_channels(raw.get("input_channels", [0]), "input_channels", name),
                in_gain_db=_gain(raw.get("in_gain_db", 0.0), "in_gain_db", name),
                output_device=out_dev,
                output_channels=_channels(raw.get("output_channels", [0, 1]), "output_channels", name),
                out_gain_db=_gain(raw.get("out_gain_db", 0.0), "out_gain_db", name),
                tone_hz=float(raw["tone_hz"]) if raw.get("tone_hz") else None,
            )
        )
    if not lines:
        raise ConfigError("no enabled [[line]] configured")

    return Config(ServerCfg(url, tuple(ice)), AudioCfg(backend, playout), tuple(lines))


def load_config(path: str | Path, env: Mapping[str, str] | None = None,
                lookup_password: Callable[[str, str], str | None] | None = None) -> Config:
    p = Path(path)
    try:
        with p.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {p}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {p}: {e}") from None
    return parse_config(data, env, lookup_password)
