"""Configuration of the SIP service: TOML -> validated, immutable dataclasses.

The `[server]` and `[[line]]` tables are the gateway's own (accounts, passwords, ...), parsed by the gateway's
code; sound card settings do not exist here. A `[[call]]` ties one SIP call to up to two lines per direction."""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from gateway.config import AudioCfg, ConfigError, DebugCfg, LineCfg, ServerCfg, parse_config

from .codecs import AudioFormat, CODECS, CodecError, MODES, validate_format
from .registration import RegisterCfg
from .sip import SipError, parse_uri

__all__ = ["ConfigError", "SipCfg", "ChannelCfg", "CallCfg", "SipConfig", "parse_sip_config", "load_sip_config"]


@dataclass(frozen=True)
class SipCfg:
    sip_port: int = 5060
    local_ip: str | None = None  # the address we put into SIP and SDP; None = the one the OS would use towards the peer
    bind_ip: str = "0.0.0.0"
    rtp_port_min: int = 5004  # EBU Tech 3326 suggests 5004 / 5005
    rtp_port_max: int = 5100
    user: str = "intercom"  # user part of our SIP address (without a registration)
    display_name: str = "Open Intercom"


@dataclass(frozen=True)
class WebCfg:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8088
    user: str = "admin"
    password: str = ""  # required unless the page is only reachable from this machine

    @property
    def local_only(self) -> bool:
        return self.host in ("127.0.0.1", "localhost", "::1")


@dataclass(frozen=True)
class ChannelCfg:
    send: str | None = None  # line whose sound is sent to the other side on this channel
    receive: str | None = None  # line this channel from the other side is spoken into


@dataclass(frozen=True)
class CallCfg:
    name: str
    mode: str  # "dial": we call `target` (by ourselves); "answer": we wait for a call; "both": we wait, and call when asked
    fmt: AudioFormat
    channels: tuple[ChannelCfg, ...]  # one per channel of the codec: left, right
    target: str | None = None
    account: str | None = None  # the SIP account (a [[account]]) this call uses: registered there, calls go through it
    accept_from: tuple[str, ...] = ()  # IP addresses (or host names, looked up on every call) allowed to call us;
    #                                    with an account, the account's registrar is allowed as well
    auth_user: str | None = None
    auth_password: str = ""
    redial: bool = True
    redial_s: float = 10.0
    rtp_timeout_s: float = 0.0  # nothing received for this long -> hang up and call again; 0 = never (PTT lines stay quiet for minutes)
    keep_lines: bool = False  # keep the lines joined between calls (default: only while a call is up)
    rtp_latch: bool = False  # send to wherever the other side's RTP comes from (NAT), instead of what its SDP says
    playout_ms: int = 60  # jitter buffer for what arrives from the other side
    autostart: bool = True  # "dial" profiles call by themselves at start (and again); False = only when asked on the status page
    in_gain_db: float = 0.0  # from the call into the lines
    out_gain_db: float = 0.0  # from the lines into the call

    @property
    def line_names(self) -> tuple[str, ...]:
        seen: list[str] = []
        for c in self.channels:
            for n in (c.send, c.receive):
                if n and n not in seen:
                    seen.append(n)
        return tuple(seen)


@dataclass(frozen=True)
class SipConfig:
    server: ServerCfg
    audio: AudioCfg
    sip: SipCfg
    accounts: dict[str, RegisterCfg]
    lines: dict[str, LineCfg]
    calls: tuple[CallCfg, ...]
    debug: DebugCfg = DebugCfg()
    web: WebCfg = WebCfg()


def _num(raw: Mapping, key: str, default, lo: float, hi: float, where: str):
    try:
        v = float(raw.get(key, default))
    except (TypeError, ValueError):
        raise ConfigError(f"{where}: {key} must be a number") from None
    if not lo <= v <= hi:
        raise ConfigError(f"{where}: {key} must be between {lo:g} and {hi:g}")
    return v


def _rtp_timeout(raw: Mapping, where: str) -> float:
    v = _num(raw, "rtp_timeout_s", 0.0, 0, 3600, where)
    if 0 < v < 3:
        raise ConfigError(f"{where}: rtp_timeout_s must be 0 (off) or at least 3")
    return v


def _web(raw: Mapping, env: Mapping[str, str]) -> WebCfg:
    listen = str(raw.get("listen", "127.0.0.1:8088"))
    host, _, port = listen.rpartition(":")
    if not host or not port.isdigit() or not 0 <= int(port) <= 65535:
        raise ConfigError('[web] listen must look like "127.0.0.1:8088"')
    password = ""
    if raw.get("password_env"):
        var = str(raw["password_env"])
        if not env.get(var):
            raise ConfigError(f"[web]: environment variable {var} is not set")
        password = env[var]
    elif raw.get("password"):
        password = str(raw["password"])
    cfg = WebCfg(bool(raw.get("enabled", True)), host.strip("[]"), int(port), str(raw.get("user", "admin")), password)
    if cfg.enabled and not cfg.local_only and not password:
        raise ConfigError("[web]: a page that other machines can reach needs a password (password_env)")
    return cfg


def _channel(raw: Mapping, name: str, side: str) -> ChannelCfg:
    where = f"call {name!r} [{side}]"
    unknown = set(raw) - {"line", "send", "receive"}
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {sorted(unknown)} (use line, send, receive)")
    line = raw.get("line")
    send, receive = raw.get("send", line), raw.get("receive", line)
    for key, v in (("line", line), ("send", send), ("receive", receive)):
        if v is not None and (not isinstance(v, str) or not v.strip()):
            raise ConfigError(f"{where}: {key} must be a line name")
    return ChannelCfg(send, receive)


def parse_sip_config(data: Mapping, env: Mapping[str, str] | None = None,
                     lookup_password: Callable[[str, str], str | None] | None = None) -> SipConfig:
    env = os.environ if env is None else env
    base = {k: v for k, v in data.items() if k in ("server", "line", "debug")}
    base["audio"] = {"backend": "synthetic", "playout_ms": (data.get("audio") or {}).get("playout_ms", 60)}
    gw = parse_config(base, env, lookup_password)  # servers, accounts and passwords exactly as the gateway reads them
    lines = {l.name: l for l in gw.lines}

    sip_raw = data.get("sip") or {}
    sip = SipCfg(
        sip_port=int(_num(sip_raw, "sip_port", 5060, 0, 65535, "[sip]")),
        local_ip=str(sip_raw["local_ip"]) if sip_raw.get("local_ip") else None,
        bind_ip=str(sip_raw.get("bind_ip", "0.0.0.0")),
        rtp_port_min=int(_num(sip_raw, "rtp_port_min", 5004, 1024, 65535, "[sip]")),
        rtp_port_max=int(_num(sip_raw, "rtp_port_max", 5100, 1024, 65535, "[sip]")),
        user=str(sip_raw.get("user", "intercom")),
        display_name=str(sip_raw.get("display_name", "Open Intercom")),
    )
    if sip.rtp_port_max < sip.rtp_port_min + 1:
        raise ConfigError("[sip] rtp_port_max must be above rtp_port_min")
    accounts: dict[str, RegisterCfg] = {}
    for raw in data.get("account", []):
        name = str(raw.get("name", "")).strip()
        if not name:
            raise ConfigError("every [[account]] needs a name")
        if name in accounts:
            raise ConfigError(f"duplicate account name {name!r}")
        if not raw.get("enabled", True):
            continue
        where = f"account {name!r}"
        for key in ("registrar", "user"):
            if not raw.get(key):
                raise ConfigError(f"{where}: {key} is required")
        password = ""
        if raw.get("password_env"):
            var = str(raw["password_env"])
            if not env.get(var):
                raise ConfigError(f"{where}: environment variable {var} is not set")
            password = env[var]
        elif raw.get("password"):
            password = str(raw["password"])
        try:
            parse_uri(str(raw["registrar"]))
        except SipError as e:
            raise ConfigError(f"{where}: {e.reason}") from None
        accounts[name] = RegisterCfg(
            registrar=str(raw["registrar"]), user=str(raw["user"]), password=password,
            domain=str(raw["domain"]) if raw.get("domain") else None,
            auth_user=str(raw["auth_user"]) if raw.get("auth_user") else None,
            expires_s=int(_num(raw, "expires_s", 300, 60, 3600, where)), name=name,
            contact_user=str(raw.get("contact_user") or re.sub(r"[^A-Za-z0-9_.-]", "-", name.lower())))
    contacts = [a.contact_user for a in accounts.values()]
    if len(set(contacts)) != len(contacts):
        raise ConfigError("two accounts have the same contact_user - incoming calls could not be told apart")

    calls: list[CallCfg] = []
    names: set[str] = set()
    used_by: dict[str, str] = {}
    for raw in data.get("call", []):
        name = str(raw.get("name", "")).strip()
        if not name:
            raise ConfigError("every [[call]] needs a name")
        if name in names:
            raise ConfigError(f"duplicate call name {name!r}")
        names.add(name)
        if not raw.get("enabled", True):
            continue
        where = f"call {name!r}"
        mode = str(raw.get("mode", "dial"))
        if mode not in ("dial", "answer", "both"):
            raise ConfigError(f"{where}: mode must be 'dial', 'answer' or 'both'")
        target = str(raw["target"]).strip() if raw.get("target") else None
        account = str(raw["account"]) if raw.get("account") else None
        if account and account not in accounts:
            raise ConfigError(f"{where}: unknown account {account!r} (defined: {', '.join(accounts) or 'none'})")
        accept = raw.get("accept_from", [])
        if isinstance(accept, str):
            accept = [accept]
        if not isinstance(accept, list) or not all(isinstance(a, str) for a in accept):
            raise ConfigError(f"{where}: accept_from must be a list of IP addresses")
        if mode == "dial":
            if not target:
                raise ConfigError(f"{where}: target is required (e.g. \"sip:codec@192.0.2.10:5060\")")
            if account and "@" not in target and not target.startswith("sip:"):
                target = f"sip:{target}@{accounts[account].domain or parse_uri(accounts[account].registrar).host}"
            try:
                parse_uri(target)
            except SipError as e:
                raise ConfigError(f"{where}: {e.reason}") from None
        else:
            if not accept and not account:
                raise ConfigError(f"{where}: an answering call needs an account or accept_from (who may call us)")
            if target:  # the number the page offers to call
                if account and "@" not in target and not target.startswith("sip:"):
                    target = f"sip:{target}@{accounts[account].domain or parse_uri(accounts[account].registrar).host}"
                try:
                    parse_uri(target)
                except SipError as e:
                    raise ConfigError(f"{where}: {e.reason}") from None

        codec = str(raw.get("codec", "mp2")).lower()
        if codec not in CODECS:
            raise ConfigError(f"{where}: codec must be one of {', '.join(CODECS)}")
        spec = CODECS[codec]
        channel_mode = str(raw.get("channel_mode", "dual-mono" if codec == "mp2" else
                                   ("mono" if spec.channels == (1,) else "stereo")))
        if channel_mode not in MODES:
            raise ConfigError(f"{where}: channel_mode must be one of {', '.join(MODES)}")
        sample_rate = int(_num(raw, "sample_rate", spec.sample_rates[-1], 8000, 96000, where))
        fmt = AudioFormat(codec, sample_rate, 1 if channel_mode == "mono" else 2,
                          int(_num(raw, "bitrate_kbps", 192 if channel_mode != "mono" else 128, 0, 512, where))
                          if codec == "mp2" else 0, channel_mode, int(_num(raw, "ptime_ms", 20, 10, 60, where)))
        try:
            validate_format(fmt)
        except CodecError as e:
            raise ConfigError(f"{where}: {e}") from None

        chans = (_channel(raw.get("left") or {}, name, "left"), _channel(raw.get("right") or {}, name, "right"))
        if fmt.channels == 1 and (chans[1].send or chans[1].receive):
            raise ConfigError(f"{where}: the codec carries one channel, but [right] names a line")
        chans = chans[:fmt.channels]
        if not any(c.send or c.receive for c in chans):
            raise ConfigError(f"{where}: no line is assigned (use [call.left] / [call.right])")
        for c in chans:
            for ln in (c.send, c.receive):
                if ln and ln not in lines:
                    raise ConfigError(f"{where}: unknown line {ln!r} (defined: {', '.join(lines) or 'none'})")
        # a line is joined with its own account, so it can serve one call only (within a call, several channels
        # may use the same line: what is spoken into it is mixed, what is sent from it is repeated)
        for ln in dict.fromkeys(n for c in chans for n in (c.receive, c.send) if n):
            other = used_by.get(ln)
            if other and other != name:
                raise ConfigError(f"{where}: line {ln!r} is already used by call {other!r} - every line is joined "
                                  "with its own account and can serve one call only")
            used_by[ln] = name

        password = ""
        if raw.get("auth_password_env"):
            var = str(raw["auth_password_env"])
            if not env.get(var):
                raise ConfigError(f"{where}: environment variable {var} is not set")
            password = env[var]
        elif raw.get("auth_password"):
            password = str(raw["auth_password"])
        if raw.get("auth_user") and not password:
            raise ConfigError(f"{where}: auth_user needs auth_password_env")

        calls.append(CallCfg(
            name=name, mode=mode, fmt=fmt, channels=chans, target=target, accept_from=tuple(accept),
            account=account, autostart=bool(raw.get("autostart", True)),
            auth_user=str(raw["auth_user"]) if raw.get("auth_user") else None, auth_password=password,
            redial=bool(raw.get("redial", True)), redial_s=_num(raw, "redial_s", 10.0, 1, 3600, where),
            rtp_timeout_s=_rtp_timeout(raw, where), keep_lines=bool(raw.get("keep_lines", False)),
            rtp_latch=bool(raw.get("rtp_latch", False)), playout_ms=int(_num(raw, "playout_ms", 60, 20, 500, where)),
            in_gain_db=_num(raw, "in_gain_db", 0.0, -40, 40, where), out_gain_db=_num(raw, "out_gain_db", 0.0, -40, 40, where),
        ))
    if not calls:
        raise ConfigError("no enabled [[call]] configured")
    return SipConfig(gw.server, gw.audio, sip, accounts, lines, tuple(calls), gw.debug, _web(data.get("web") or {}, env))


def load_sip_config(path: str | Path, env: Mapping[str, str] | None = None,
                    lookup_password: Callable[[str, str], str | None] | None = None) -> SipConfig:
    p = Path(path)
    try:
        with p.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {p}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {p}: {e}") from None
    return parse_sip_config(data, env, lookup_password)
