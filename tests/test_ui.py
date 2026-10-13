"""UI tests, run offscreen. The start/stop test spawns a real detached gateway (against an unreachable server,
synthetic audio), so it covers the UI -> launcher -> process -> control channel -> UI loop."""
import os
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from gateway import tomlio  # noqa: E402
from gateway.control import find_running  # noqa: E402
from gateway.ui.settings import LINE_URL, DeviceCombo, parse_channels  # noqa: E402
from gateway.ui.window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def pump(app, seconds=0.0, until=None, timeout=15.0):
    end = time.monotonic() + (timeout if until else seconds)
    while time.monotonic() < end:
        app.processEvents()
        if until and until():
            return True
        time.sleep(0.02)
    return False


def write_cfg(path):
    tomlio.save(path, {
        "server": {"url": "http://127.0.0.1:9"},  # nothing listens here
        "audio": {"backend": "synthetic", "playout_ms": 60},
        "line": [{"name": "Regie", "production_id": "1", "line_id": "1", "username": "u", "password": "p"}],
    })


def test_parse_channels():
    assert parse_channels("1") == [0]
    assert parse_channels("2, 1") == [0, 1]
    for bad in ("", "0", "a", "1,-2"):
        with pytest.raises(ValueError):
            parse_channels(bad)


def test_line_link_is_parsed():
    m = LINE_URL.search("https://intercom.example.com/production-calls/production/4/line/1")
    assert m.groups() == ("4", "1")


def test_device_combo_value(app):
    c = DeviceCombo([("DVS Receive 1-2", 2)], "DVS Receive 9-10")  # configured device is absent right now
    assert c.value() == "DVS Receive 9-10"  # kept, not silently replaced
    c.setCurrentIndex(c.findData("DVS Receive 1-2"))
    assert c.value() == "DVS Receive 1-2"
    c.setCurrentIndex(0)
    assert c.value() == ""
    c.setEditText("DVS Receive 3")  # hand-typed fragment is passed through
    assert c.value() == "DVS Receive 3"


def test_stopped_window_shows_configured_lines(app, tmp_path):
    cfg = tmp_path / "gateway.toml"
    write_cfg(cfg)
    win = MainWindow(tmp_path, cfg)
    try:
        pump(app, 0.6)
        assert win.headline.text() == "Gateway gestoppt"
        assert list(win._cards) == ["Regie"]
        assert win.btn_start.isEnabled() and not win.btn_stop.isEnabled()
    finally:
        win.close()


def test_start_and_stop_through_the_window_and_gateway_outlives_it(app, tmp_path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.Yes)
    cfg = tmp_path / "gateway.toml"
    write_cfg(cfg)
    win = MainWindow(tmp_path, cfg)
    try:
        win.btn_start.click()
        assert pump(app, until=lambda: win._running and not win._busy), "gateway did not come up"
        assert win.headline.text() == "Gateway läuft"
        assert pump(app, until=lambda: win._cards["Regie"].pill.text() != "Aus", timeout=5)
        assert not win.btn_start.isEnabled() and win.btn_stop.isEnabled()
        pid = find_running(tmp_path)["pid"]

        win.close()  # closing the window must not stop the gateway
        time.sleep(1.0)
        assert find_running(tmp_path)["pid"] == pid

        win = MainWindow(tmp_path, cfg)  # a new window attaches to the running gateway
        assert pump(app, until=lambda: win._running)
        win.btn_stop.click()
        assert pump(app, until=lambda: not win._running and not win._busy), "gateway did not stop"
        assert find_running(tmp_path) is None
    finally:
        win.close()
        from gateway import launcher

        launcher.stop(tmp_path)  # never leave a stray process behind


def test_start_with_bad_config_shows_error_instead_of_spawning(app, tmp_path):
    cfg = tmp_path / "gateway.toml"
    tomlio.save(cfg, {"server": {"url": "nope"}, "audio": {"backend": "synthetic"},
                      "line": [{"name": "A", "production_id": "1", "line_id": "1", "username": "u", "password": "p"}]})
    win = MainWindow(tmp_path, cfg)
    try:
        win.btn_start.click()
        assert pump(app, until=lambda: not win._busy and win.banner.isVisibleTo(win), timeout=5)
        assert "http" in win.banner.text()
        assert find_running(tmp_path) is None
    finally:
        win.close()
