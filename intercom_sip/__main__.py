"""intercom-sip run -c sip.toml | check -c sip.toml"""
from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import signal
import sys
from pathlib import Path

from gateway.auth import AuthClient, LoginError
from gateway.http import create_client

from . import __version__
from .calls import SipService
from .config import ConfigError, SipConfig, load_sip_config
from .registration import Registration
from .sip import SipEndpoint, SipError, parse_uri

log = logging.getLogger("sip")


def setup_logging(verbose: bool, logfile: str | None) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    if logfile:
        Path(logfile).expanduser().parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(logfile, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    for noisy in ("aiortc", "aioice", "httpx", "httpcore", "libav"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def cmd_check(cfg: SipConfig) -> int:
    ok = True
    http = await create_client(cfg.server.url)
    print(f"server: {cfg.server.url}")
    ua = SipEndpoint(0, cfg.sip.user, cfg.sip.display_name, cfg.sip.local_ip, cfg.sip.bind_ip)
    await ua.start()
    try:
        for name, acc in cfg.accounts.items():
            reg = Registration(ua, acc)
            try:
                await reg.register(60)
                await reg.register(0)
                print(f"{chr(10)}account {name!r}: {reg.address} at {acc.registrar} OK (registered and removed again)")
            except (SipError, OSError) as e:
                print(f"{chr(10)}account {name!r}: {reg.address} at {acc.registrar} ERROR {e}")
                ok = False
        for call in cfg.calls:
            f = call.fmt
            print(f"\ncall {call.name!r} ({call.mode}): {f.describe()}"
                  + (f", ptime {f.ptime_ms} ms" if not f.spec.frame_based else ""))
            for i, ch in enumerate(call.channels):
                side = ("left", "right")[i]
                print(f"  {side:5}: sends {ch.send or '-'}   receives into {ch.receive or '-'}")
            if call.mode == "dial":
                try:
                    uri = parse_uri(call.target or "")
                    addr = await ua.resolve(uri.host, uri.port)
                    print(f"  target: {uri} -> {addr[0]}:{addr[1]}, we would use {cfg.sip.local_ip or ua.local_ip_for(addr)}")
                    try:
                        status = await ua.options(call.target or "", timeout=3.0)
                        print(f"  OPTIONS: answered {status} (the other side is there)")
                    except SipError as e:
                        print(f"  OPTIONS: no answer ({e}) - many codecs do not answer OPTIONS; a call may still work")
                except SipError as e:
                    print(f"  target: ERROR {e}")
                    ok = False
            else:
                print(f"  accepts calls from: {', '.join(call.accept_from)}")
            for name in call.line_names:
                line = cfg.lines[name]
                try:
                    await AuthClient(http, line.username, line.password).token()
                    print(f"  line {name!r} (production {line.production_id}, line {line.line_id}): login OK as {line.username}")
                except LoginError as e:
                    print(f"  line {name!r}: login ERROR {e}")
                    ok = False
    finally:
        await ua.close()
        await http.aclose()
    print("\nall good" if ok else "\nproblems found")
    return 0 if ok else 1


async def cmd_run(cfg: SipConfig, status_every: float, seconds: float | None) -> int:
    loop = asyncio.get_running_loop()
    http = await create_client(cfg.server.url)
    service = SipService(cfg, http)
    try:
        await service.start()
    except OSError as e:
        print(f"cannot open SIP port {cfg.sip.sip_port}: {e}", file=sys.stderr)
        await http.aclose()
        return 3
    if seconds:
        loop.call_later(seconds, service.stop)

    def request_stop(*_):
        loop.call_soon_threadsafe(service.stop)

    for sig in (signal.SIGINT, getattr(signal, "SIGBREAK", signal.SIGTERM), signal.SIGTERM):
        try:
            signal.signal(sig, request_stop)
        except (ValueError, OSError):
            pass

    async def report():
        while True:
            await asyncio.sleep(status_every)
            for reg in service.status()["registrations"]:
                log.info("account %s (%s): %s%s", reg["account"], reg["address"], reg["state"],
                         f" ({reg['last_error']})" if reg["last_error"] else "")
            for s in service.status()["calls"]:
                m = s["media"] or {}
                log.info("[%s] %s%s%s%s", s["call"], s["state"],
                         f"  up {s['uptime_s']} s  tx {m.get('tx_dbfs')} dBFS  rx {m.get('rx_dbfs')} dBFS"
                         f"  lost {m.get('rx_lost')} underruns {m.get('rx_underruns')}" if m else "",
                         "  lines: " + ", ".join(f"{n} {l['state']}" for n, l in s["lines"].items()) if s["lines"] else "",
                         f"  last error: {s['last_error']}" if s["last_error"] else "")

    reporter = asyncio.ensure_future(report())
    try:
        await service.run()
    finally:
        reporter.cancel()
        await service.close()
        await http.aclose()
    log.info("stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="intercom-sip", description=__doc__)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="place / answer the configured calls and keep them up")
    p_chk = sub.add_parser("check", help="validate the config, reach the targets and log the line accounts in")
    for p in (p_run, p_chk):
        p.add_argument("-c", "--config", default="sip.toml")
        p.add_argument("-v", "--verbose", action="store_true")
    p_run.add_argument("--log-file", default="sip.log")
    p_run.add_argument("--status-every", type=float, default=30.0)
    p_run.add_argument("--seconds", type=float, default=None, help="stop by itself after this many seconds (for tests)")
    a = ap.parse_args(argv)
    setup_logging(a.verbose, getattr(a, "log_file", None))
    try:
        cfg = load_sip_config(a.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if a.cmd == "check":
        return asyncio.run(cmd_check(cfg))
    return asyncio.run(cmd_run(cfg, a.status_every, a.seconds))


if __name__ == "__main__":
    sys.exit(main())
