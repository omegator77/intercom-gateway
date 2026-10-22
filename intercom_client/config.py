"""Client configuration: TOML file -> validated, immutable dataclasses."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from gateway import credentials

from .hotkeys import Combo, HotkeyError, HotkeyMatcher, parse_combo


class ConfigError(Exception):
    pass


VOLUME_RANGE = (0, 150)  # percent


@dataclass(frozen=True)
class ClientAudio:
    backend: str = "portaudio"  # "portaudio" or "synthetic" (a test tone in, nothing out: for tests)
    input_device: str | None = None  # name or part of it; None = the system default
    output_device: str | None = None
    input_channel: int = 0  # which channel of the microphone device
    output_channels: tuple[int, ...] = (0, 1)  # the mono mix is played on these
    mic_gain_db: float = 0.0
    output_gain_db: float = 0.0
    playout_ms: int = 60
    # Microphone processing (WebRTC audio processing, the same family the browser uses). All off = unprocessed.
    echo_cancellation: bool = False  # for speakers instead of a headset
    noise_suppression: int = 0  # 0 off, 1 low, 2 moderate, 3 high, 4 very high
    auto_gain: bool = False


@dataclass(frozen=True)
class ClientDim:
    """Lowering the programme line while somebody talks (same behaviour and defaults as the web app)."""
    amount_db: int = -14  # 0 = never dimmed
    when_addressed: bool = True  # someone speaks to me on another line
    while_speaking: bool = True  # I speak on another line
    delay_ms: int = 1000  # speech must last this long before dimming starts, and it stays this long after it ended


@dataclass(frozen=True)
class ClientLine:
    name: str
    production_id: str
    line_id: str
    volume_pct: int = 100
    ptt_key: str | None = None  # talk while held
    latch_key: str | None = None  # toggle "keep talking"


@dataclass(frozen=True)
class ClientCfg:
    server_url: str
    username: str
    password: str
    audio: ClientAudio
    lines: tuple[ClientLine, ...]
    global_mute_key: str | None = None
    companion_url: str | None = None  # ws://host:port of the Companion module (Stream Deck), None = off
    dim: ClientDim = ClientDim()

    def hotkey_bindings(self) -> dict[str, Combo]:
        """action id -> key combination, for every configured key."""
        b: dict[str, Combo] = {}
        for line in self.lines:
            if line.ptt_key:
                b[f"ptt:{line.name}"] = parse_combo(line.ptt_key)
            if line.latch_key:
                b[f"latch:{line.name}"] = parse_combo(line.latch_key)
        if self.global_mute_key:
            b["global_mute"] = parse_combo(self.global_mute_key)
        return b


def _key(value, what: str) -> str | None:
    if value in (None, ""):
        return None
    try:
        parse_combo(str(value))
    except ValueError as e:
        raise ConfigError(f"{what}: {e}") from None
    return str(value).strip()


def _gain(value, what: str) -> float:
    try:
        g = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{what} must be a number (dB)") from None
    if not -40.0 <= g <= 40.0:
        raise ConfigError(f"{what} must be between -40 and +40 dB")
    return g


def parse_client_config(data: Mapping, env: Mapping[str, str] | None = None,
                        lookup_password: Callable[[str, str], str | None] | None = None) -> ClientCfg:
    env = os.environ if env is None else env
    server = data.get("server") or {}
    url = str(server.get("url", "")).strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ConfigError("[server] url must start with http:// or https://")
    username = str(server.get("username", "")).strip()
    if not username:
        raise ConfigError("[server] username is required")

    password = env.get("INTERCOM_CLIENT_PASSWORD") or str(server.get("password") or "")
    if not password:
        password = (lookup_password or credentials.get_password)(url, username) or ""
    if not password:
        raise ConfigError("no password - enter it in the settings (kept in the Windows Credential Manager)")

    a = data.get("audio") or {}
    backend = str(a.get("backend", "portaudio"))
    if backend not in ("portaudio", "synthetic"):
        raise ConfigError("[audio] backend must be 'portaudio' or 'synthetic'")
    playout = int(a.get("playout_ms", 60))
    if not 20 <= playout <= 500:
        raise ConfigError("[audio] playout_ms must be between 20 and 500")
    in_ch = a.get("input_channel", 0)
    if not isinstance(in_ch, int) or isinstance(in_ch, bool) or in_ch < 0:
        raise ConfigError("[audio] input_channel must be a number >= 0")
    out_ch = a.get("output_channels", [0, 1])
    if not isinstance(out_ch, list) or not out_ch or not all(isinstance(c, int) and c >= 0 for c in out_ch):
        raise ConfigError("[audio] output_channels must be a non-empty list of channel numbers >= 0")
    ns = a.get("noise_suppression", 0)
    if not isinstance(ns, int) or isinstance(ns, bool) or not 0 <= ns <= 4:
        raise ConfigError("[audio] noise_suppression must be 0 (off) .. 4 (very high)")
    for key in ("echo_cancellation", "auto_gain"):
        if not isinstance(a.get(key, False), bool):
            raise ConfigError(f"[audio] {key} must be true or false")
    audio = ClientAudio(backend, a.get("input_device") or None, a.get("output_device") or None, in_ch,
                        tuple(out_ch), _gain(a.get("mic_gain_db", 0.0), "[audio] mic_gain_db"),
                        _gain(a.get("output_gain_db", 0.0), "[audio] output_gain_db"), playout,
                        bool(a.get("echo_cancellation", False)), ns, bool(a.get("auto_gain", False)))

    d = data.get("dim") or {}
    amount = d.get("amount_db", -14)
    if not isinstance(amount, int) or isinstance(amount, bool) or not -40 <= amount <= 0:
        raise ConfigError("[dim] amount_db must be a whole number between -40 and 0 (0 = no dimming)")
    delay = d.get("delay_ms", 1000)
    if not isinstance(delay, int) or isinstance(delay, bool) or not 0 <= delay <= 5000:
        raise ConfigError("[dim] delay_ms must be between 0 and 5000")
    for key in ("when_addressed", "while_speaking"):
        if not isinstance(d.get(key, True), bool):
            raise ConfigError(f"[dim] {key} must be true or false")
    dim = ClientDim(amount, bool(d.get("when_addressed", True)), bool(d.get("while_speaking", True)), delay)

    lines: list[ClientLine] = []
    seen: set[str] = set()
    for raw in data.get("line", []):
        name = str(raw.get("name", "")).strip()
        if not name:
            raise ConfigError("every [[line]] needs a name")
        if name in seen:
            raise ConfigError(f"duplicate line name {name!r}")
        seen.add(name)
        for k in ("production_id", "line_id"):
            if raw.get(k) in (None, ""):
                raise ConfigError(f"line {name!r}: {k} is required")
        vol = raw.get("volume", 100)
        if not isinstance(vol, int) or isinstance(vol, bool) or not VOLUME_RANGE[0] <= vol <= VOLUME_RANGE[1]:
            raise ConfigError(f"line {name!r}: volume must be a whole percent between {VOLUME_RANGE[0]} and {VOLUME_RANGE[1]}")
        lines.append(ClientLine(name, str(raw["production_id"]), str(raw["line_id"]), vol,
                                _key(raw.get("ptt_key"), f"line {name!r}: ptt_key"),
                                _key(raw.get("latch_key"), f"line {name!r}: latch_key")))
    if not lines:
        raise ConfigError("no [[line]] configured")

    hk = data.get("hotkeys") or {}
    comp = (data.get("companion") or {}).get("url") or None
    if comp is not None and not str(comp).startswith(("ws://", "wss://")):
        raise ConfigError("[companion] url must start with ws:// or wss://")
    cfg = ClientCfg(url, username, password, audio, tuple(lines),
                    _key(hk.get("global_mute"), "[hotkeys] global_mute"), comp, dim)
    try:
        HotkeyMatcher(cfg.hotkey_bindings())  # raises when one key combination is bound twice
    except HotkeyError as e:
        raise ConfigError(str(e)) from None
    return cfg


def load_client_config(path: str | Path, env: Mapping[str, str] | None = None,
                       lookup_password: Callable[[str, str], str | None] | None = None) -> ClientCfg:
    p = Path(path)
    try:
        with p.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {p}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {p}: {e}") from None
    return parse_client_config(data, env, lookup_password)
