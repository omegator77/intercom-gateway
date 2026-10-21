"""How the UI starts the detached gateway process, from source and from the packaged executable."""
import sys
from pathlib import Path

from gateway import launcher


def test_command_from_source_runs_the_module(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    cmd = launcher.gateway_command(tmp_path / "gateway.toml", tmp_path)
    assert cmd[1:4] == ["-m", "gateway", "run"]
    assert cmd[cmd.index("-c") + 1] == str(tmp_path / "gateway.toml")
    assert cmd[cmd.index("--state-dir") + 1] == str(tmp_path)
    assert cmd[cmd.index("--log-file") + 1] == str(tmp_path / "gateway.log")


def test_command_when_packaged_reruns_the_executable(tmp_path, monkeypatch):
    exe = str(Path("C:/app/IntercomGateway.exe"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", exe)
    cmd = launcher.gateway_command(tmp_path / "gateway.toml", tmp_path)
    assert cmd[:2] == [exe, "--gateway-run"]
    assert "-m" not in cmd  # a packaged build has no `python -m gateway`
    assert cmd[cmd.index("-c") + 1] == str(tmp_path / "gateway.toml")
    assert cmd[cmd.index("--log-file") + 1] == str(tmp_path / "gateway.log")
