"""intercom-gateway run -c gateway.toml | devices | check -c gateway.toml"""
from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import signal
import sys
import time
from pathlib import Path

from . import __version__
from .audio import DeviceError, make_line_audio, match_device
from .auth import AuthClient, LoginError
from .config import Config, ConfigError, load_config
from .control import ControlServer, fetch_status, find_running, request_stop
from .http import create_client
from .paths import state_dir
from .session import LineWorker

log = logging.getLogger("gateway")


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
    for noisy in ("aiortc", "aioice", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_devices() -> int:
    import sounddevice as sd

    apis = sd.query_hostapis()
    print("Only WASAPI devices are listed (that is what the gateway uses).")
    for i, d in enumerate(sd.query_devices()):
        if apis[d["hostapi"]]["name"] != "Windows WASAPI":
            continue
        kind = "capture " if d["max_input_channels"] else "playback"
        ch = d["max_input_channels"] or d["max_output_channels"]
        print(f"  [{i:3}] {kind} {ch:2} ch  {d['name']}")
    return 0


async def cmd_check(cfg: Config) -> int:
    ok = True
    http = await create_client(cfg.server.url)
    print(f"server: {cfg.server.url}   audio backend: {cfg.audio.backend}")
    if cfg.audio.backend == "portaudio":
        import sounddevice as sd

        devices, apis = sd.query_devices(), sd.query_hostapis()
    for line in cfg.lines:
        print(f"\nline {line.name!r} (production {line.production_id}, line {line.line_id}, account {line.username})")
        if cfg.audio.backend == "portaudio":
            for kind, name, chans in (("input", line.input_device, line.input_channels),
                                      ("output", line.output_device, line.output_channels)):
                if not name:
                    continue
                try:
                    idx = match_device(devices, apis, name, kind)
                    have = devices[idx]["max_input_channels" if kind == "input" else "max_output_channels"]
                    good = max(chans) < have
                    print(f"  {kind:6}: {devices[idx]['name']}  ({have} ch, using {list(chans)})  {'OK' if good else 'TOO FEW CHANNELS'}")
                    ok &= good
                except DeviceError as e:
                    print(f"  {kind:6}: ERROR {e}")
                    ok = False
        try:
            await AuthClient(http, line.username, line.password).token()
            print("  login : OK")
        except LoginError as e:
            print(f"  login : ERROR {e}")
            ok = False
    await http.aclose()
    print("\nall good" if ok else "\nproblems found")
    return 0 if ok else 1


async def cmd_run(cfg: Config, status_every: float, seconds: float | None = None, home: Path | None = None,
                  config_path: str = "") -> int:
    home = home or state_dir()
    running = find_running(home)
    if running:
        print(f"a gateway is already running (pid {running['pid']}); stop it first: intercom-gateway stop", file=sys.stderr)
        return 3
    loop = asyncio.get_running_loop()
    http = await create_client(cfg.server.url)
    workers = [LineWorker(l, cfg.server, cfg.audio, make_line_audio, http) for l in cfg.lines]
    if seconds:
        log.info("will stop by itself after %.0f s", seconds)
        loop.call_later(seconds, lambda: [w.stop() for w in workers])
    for w in workers:
        log.info("line %r -> production %s line %s as %s", w.cfg.name, w.cfg.production_id, w.cfg.line_id, w.cfg.username)

    def request_stop(*_):
        loop.call_soon_threadsafe(lambda: [w.stop() for w in workers])

    started = time.time()

    def full_status() -> dict:
        return {"pid": os.getpid(), "version": __version__, "started_at": started, "server": cfg.server.url,
                "config": config_path, "lines": [w.status() for w in workers]}

    control = ControlServer(home, full_status, lambda: [w.stop() for w in workers], config_path)
    try:
        await control.start()
    except OSError as e:  # the gateway still works without it, only the UI cannot see it
        log.warning("control channel unavailable: %s", e)

    for sig in (signal.SIGINT, getattr(signal, "SIGBREAK", signal.SIGTERM), signal.SIGTERM):
        try:
            signal.signal(sig, request_stop)
        except (ValueError, OSError):
            pass

    async def report():
        while True:
            await asyncio.sleep(status_every)
            for w in workers:
                s = w.status()
                log.info("[%s] %s in %.0f dBFS out %.0f dBFS %s%s", s["line"], s["state"], s["in_dbfs"], s["out_dbfs"],
                         s["audio"] or "", f"  last error: {s['last_error']}" if s["last_error"] else "")

    reporter = asyncio.ensure_future(report())
    try:
        await asyncio.gather(*[w.run() for w in workers])
    finally:
        reporter.cancel()
        await control.close()
        await http.aclose()
    log.info("stopped")
    return 0


def cmd_status(home: Path) -> int:
    info = find_running(home)
    if not info:
        print("gateway is not running")
        return 1
    s = fetch_status(info)
    print(f"gateway running (pid {s['pid']}, version {s['version']}, config {s['config'] or '-'})")
    for l in s["lines"]:
        print(f"  {l['line']:16} {l['state']:10} in {l['in_dbfs']:6.1f} dBFS  out {l['out_dbfs']:6.1f} dBFS"
              f"  up {l['uptime_s']} s" + (f"  last error: {l['last_error']}" if l["last_error"] else ""))
    return 0


def cmd_stop(home: Path, wait_s: float = 20.0) -> int:
    info = find_running(home)
    if not info:
        print("gateway is not running")
        return 1
    request_stop(info)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if not find_running(home):
            print("gateway stopped")
            return 0
        time.sleep(0.3)
    print("gateway did not stop in time", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="intercom-gateway", description=__doc__)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="connect all configured lines and keep them connected")
    p_chk = sub.add_parser("check", help="validate config, devices and logins without starting audio")
    for p in (p_run, p_chk):
        p.add_argument("-c", "--config", default="gateway.toml")
        p.add_argument("-v", "--verbose", action="store_true")
    p_run.add_argument("--log-file", default="gateway.log")
    p_run.add_argument("--status-every", type=float, default=30.0)
    p_run.add_argument("--seconds", type=float, default=None, help="stop by itself after this many seconds (for tests)")
    sub.add_parser("devices", help="list usable audio devices")
    p_st = sub.add_parser("status", help="show the state of a running gateway")
    p_sp = sub.add_parser("stop", help="stop a running gateway")
    for p in (p_run, p_st, p_sp):
        p.add_argument("--state-dir", default=None, help="where the control file lives (default: intercom-gateway folder in LOCALAPPDATA)")

    a = ap.parse_args(argv)
    if a.cmd == "devices":
        return cmd_devices()
    if a.cmd == "status":
        return cmd_status(state_dir(a.state_dir))
    if a.cmd == "stop":
        return cmd_stop(state_dir(a.state_dir))
    setup_logging(a.verbose, getattr(a, "log_file", None))
    try:
        cfg = load_config(a.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if a.cmd == "check":
        return asyncio.run(cmd_check(cfg))
    return asyncio.run(cmd_run(cfg, a.status_every, a.seconds, state_dir(a.state_dir), str(Path(a.config).resolve())))


if __name__ == "__main__":
    sys.exit(main())
