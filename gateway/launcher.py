"""Starts the gateway as an independent background process and stops it again. The process is detached
from the caller on purpose: closing or crashing the UI must not interrupt the audio."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from .config import ConfigError, load_config
from .control import ControlError, find_running, read_control, request_stop
from .paths import default_log

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


class LaunchError(Exception):
    pass


def _python_exe() -> str:
    """python.exe, even when the UI itself runs under pythonw: pythonw has no stderr, so a crash traceback
    of the gateway would vanish. DETACHED_PROCESS already keeps python.exe from opening a console."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return str(exe)


def gateway_command(config_path: Path, home: Path) -> list[str]:
    """Command line of the detached gateway process. A packaged build has no `python -m gateway`: the
    executable starts itself again in headless mode (see packaging/gui_entry.py)."""
    args = ["-c", str(config_path), "--state-dir", str(home), "--log-file", str(default_log(home))]
    if getattr(sys, "frozen", False):
        return [sys.executable, "--gateway-run", *args]
    return [_python_exe(), "-m", "gateway", "run", *args]


def start(config_path: Path, home: Path, wait_s: float = 20.0) -> dict:
    """Validate the config, spawn `gateway run` detached and wait until its control channel answers.
    Returns the control info. Raises LaunchError with a message fit to show the user."""
    if find_running(home):
        raise LaunchError("Das Gateway läuft bereits.")
    try:
        load_config(config_path)  # fail here, with a readable message, rather than in an invisible process
    except ConfigError as e:
        raise LaunchError(str(e)) from None
    home.mkdir(parents=True, exist_ok=True)
    cmd = gateway_command(config_path, home)
    err = (home / "gateway.stderr").open("ab")
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=err, stderr=err, cwd=str(home), close_fds=True)
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        try:  # leave a job object that would kill its children with the UI; not every job allows it
            proc = subprocess.Popen(cmd, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kwargs)
        except OSError:
            proc = subprocess.Popen(cmd, creationflags=flags, **kwargs)
    finally:
        err.close()
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        info = find_running(home)
        if info:
            return info
        if proc.poll() is not None:
            raise LaunchError(f"Das Gateway hat sich sofort beendet (Code {proc.returncode}). "
                              f"Details: {home / 'gateway.stderr'}")
        time.sleep(0.25)
    raise LaunchError("Das Gateway hat sich nicht rechtzeitig gemeldet. Details im Protokoll.")


def stop(home: Path, wait_s: float = 20.0) -> None:
    info = find_running(home)
    if not info:
        return
    try:
        request_stop(info)
    except ControlError as e:
        raise LaunchError(f"Stopp-Befehl fehlgeschlagen: {e}") from None
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if not find_running(home):
            return
        time.sleep(0.25)
    raise LaunchError("Das Gateway hat sich nicht rechtzeitig beendet.")


def running_info(home: Path) -> dict | None:
    return find_running(home)


__all__ = ["start", "stop", "running_info", "LaunchError", "read_control"]
