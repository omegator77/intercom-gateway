"""The client's window and settings dialog, offscreen, with a fake line worker (no network, synthetic audio)."""
import asyncio
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QKeySequence  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import intercom_client.engine as engine_mod  # noqa: E402
from gateway import tomlio  # noqa: E402
from gateway.lines import AvailableLine  # noqa: E402
from intercom_client.ui.settings import ClientSettingsDialog, KeyEdit, key_spec  # noqa: E402
from intercom_client.ui.window import MainWindow, key_hint  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def pump(app, seconds=0.0, until=None, timeout=10.0):
    end = time.monotonic() + (timeout if until else seconds)
    while time.monotonic() < end:
        app.processEvents()
        if until and until():
            return True
        time.sleep(0.02)
    return False


class FakeWorker:
    def __init__(self, cfg, server, audio_cfg, factory, http, debug=None, auth=None):
        self.cfg, self._factory, self._audio_cfg = cfg, factory, audio_cfg
        self._stop = asyncio.Event()

    async def run(self):
        audio = self._factory(self.cfg, self._audio_cfg, asyncio.get_running_loop())
        await audio.start()
        await self._stop.wait()
        await audio.stop()

    def stop(self):
        self._stop.set()

    def status(self):
        return {"state": "connected", "uptime_s": 65, "last_error": None, "in_dbfs": -30.0, "out_dbfs": -40.0,
                "participants": [{"name": "Ben <b>", "active": True, "whip": False}], "audio": {}}


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    class FakeHttp:
        async def aclose(self):
            pass

    async def fake_client(url):
        return FakeHttp()

    monkeypatch.setattr(engine_mod, "LineWorker", FakeWorker)
    monkeypatch.setattr(engine_mod, "create_client", fake_client)
    monkeypatch.setenv("INTERCOM_CLIENT_PASSWORD", "pw")


def write_cfg(path):
    tomlio.save(path, {
        "server": {"url": "https://intercom.example", "username": "anna"},
        "audio": {"backend": "synthetic"},
        "hotkeys": {"global_mute": "ctrl+alt+m"},
        "line": [{"name": "Regie", "production_id": "4", "line_id": "1", "ptt_key": "f1", "latch_key": "shift+f1",
                  "volume": 90},
                 {"name": "Technik", "production_id": "4", "line_id": "2", "volume": 100}],
    })


# ---------------------------------------------------------------- window

def test_window_shows_a_card_per_line_and_the_buttons_drive_the_engine(app, tmp_path):
    cfg = tmp_path / "client.toml"
    write_cfg(cfg)
    win = MainWindow(tmp_path, cfg, enable_hotkeys=False, autostart=False)
    try:
        win.connect_now()
        assert pump(app, until=lambda: win.engine is not None and win.engine.running and win.btn_connect.isEnabled())
        assert pump(app, until=lambda: win.cards["Regie"].pill.text() == "Verbunden")
        regie, technik = win.cards["Regie"], win.cards["Technik"]
        assert list(win.cards) == ["Regie", "Technik"]
        assert "verbunden seit 0:01:05" in regie.since.text()
        assert "Ben" in regie.people.text() and "<b>" not in regie.people.text().replace("&lt;b&gt;", "")
        assert "[F1]" in regie.talk.text() and "[SHIFT+F1]" in regie.latch.text()
        assert regie.volume.value() == 90 and regie.volume_text.text() == "90 %"
        eng = win.engine

        regie.talk.pressed.emit()
        pump(app, 0.3)
        assert eng.controls["Regie"].talk.held and "Sendet" in regie.talk.text()
        regie.talk.released.emit()
        pump(app, 0.3)
        assert not eng.controls["Regie"].talk.held and regie.talk.text().startswith("Sprechen")

        regie.latch.click()
        pump(app, 0.3)
        assert eng.controls["Regie"].talk.latched and regie.latch.isChecked() and "Dauer" in regie.latch.text()
        technik.listen.click()
        pump(app, 0.3)
        assert eng.controls["Technik"].listen_mute and technik.listen.isChecked()
        regie.volume.setValue(120)
        assert eng.controls["Regie"].volume_pct == 120

        win.btn_mute_all.click()
        pump(app, 0.3)
        assert eng.global_mute and "(CTRL+ALT+M)" in win.btn_mute_all.text()
        win.btn_mute_all.click()
        pump(app, 0.3)
        assert not eng.global_mute
    finally:
        win.close()
    assert win.engine is None


def test_window_without_a_config_says_so_and_a_broken_config_shows_the_reason(app, tmp_path):
    cfg = tmp_path / "client.toml"
    win = MainWindow(tmp_path, cfg, enable_hotkeys=False, autostart=False)
    pump(app, 0.3)
    assert win.status.text() == "Noch nicht eingerichtet"
    cfg.write_text('[server]\nurl = "ftp://x"\n', encoding="utf-8")
    win.connect_now()
    assert win.engine is None and "http" in win.warning.text() and not win.warning.isHidden()
    win.close()


def test_a_missing_device_is_shown_and_the_button_works_again(app, tmp_path):
    from gateway.audio import DeviceError
    from intercom_client.hub import SyntheticHub

    class Broken(SyntheticHub):
        async def start(self):
            raise DeviceError("no microphone found - choose one in the settings")

    cfg = tmp_path / "client.toml"
    write_cfg(cfg)
    win = MainWindow(tmp_path, cfg, enable_hotkeys=False, autostart=False, hub_factory=Broken)
    try:
        win.connect_now()
        assert pump(app, until=lambda: not win.warning.isHidden())
        assert "no microphone" in win.warning.text()
        assert win.engine is None and win.btn_connect.text() == "Verbinden" and win.btn_connect.isEnabled()
    finally:
        win.close()


def test_key_hint():
    assert key_hint(None) == "" and key_hint("ctrl+f1") == "  [CTRL+F1]"


# ---------------------------------------------------------------- settings

def test_key_spec_round_trip(app):
    for spec in ("f1", "ctrl+shift+f2", "alt+m", "space", "ctrl+alt+1"):
        edit = KeyEdit(spec)
        assert edit.spec() == spec, spec
    assert KeyEdit(None).spec() is None
    assert key_spec(QKeySequence("Ctrl+Return")) == "ctrl+enter"
    assert key_spec(QKeySequence("Meta+F5")) == "win+f5"


def dialog(tmp_path, with_cfg=True):
    cfg = tmp_path / "client.toml"
    if with_cfg:
        write_cfg(cfg)
    return ClientSettingsDialog(cfg, device_lists=([("Headset Mikrofon", 1)], [("Headset Kopfhörer", 2)]))


def test_settings_load_the_file_and_save_the_same_thing_back(app, tmp_path):
    d = dialog(tmp_path)
    assert d.url.text() == "https://intercom.example" and d.user.text() == "anna"
    assert d.table.rowCount() == 2 and d.table.item(0, 0).text() == "Regie"
    assert d.table.cellWidget(0, 2).spec() == "f1" and d.table.cellWidget(0, 3).spec() == "shift+f1"
    assert d.mute_all_key.spec() == "ctrl+alt+m"
    d.pw.setText("secret")
    d._accept()
    out = d.result_data
    assert out is not None and d.new_password == "secret" and d.error.text() == ""
    assert "password" not in out["server"]  # never in the file
    assert out["server"] == {"url": "https://intercom.example", "username": "anna"}
    assert [(l["name"], l["production_id"], l["line_id"], l["volume"]) for l in out["line"]] == [
        ("Regie", "4", "1", 90), ("Technik", "4", "2", 100)]
    assert out["line"][0]["ptt_key"] == "f1" and "ptt_key" not in out["line"][1]
    assert out["hotkeys"] == {"global_mute": "ctrl+alt+m"} and out["audio"]["backend"] == "synthetic"


def test_settings_choose_devices_add_reorder_and_remove_lines(app, tmp_path):
    d = dialog(tmp_path)
    d._show_lines([AvailableLine("4", "Show", "3", "Kamera"), AvailableLine("4", "Show", "1", "Regie")])
    d.line_pick.setCurrentIndex(0)
    d._add_picked()
    assert d.table.rowCount() == 3 and d.table.item(2, 0).text() == "Kamera"
    d.line_pick.setCurrentIndex(1)
    d._add_picked()  # Regie (4/1) is already there
    assert d.table.rowCount() == 3 and "schon" in d.error.text()

    d.table.setCurrentCell(2, 0)
    d.table.cellWidget(2, 2).setKeySequence(QKeySequence("F3"))
    d._move(-1)
    assert [d.table.item(r, 0).text() for r in range(3)] == ["Regie", "Kamera", "Technik"]
    assert d.table.cellWidget(1, 2).spec() == "f3" and d.table.cellWidget(2, 2).spec() is None  # the key moved with its row
    d.table.setCurrentCell(0, 0)
    d._remove()
    assert d.table.rowCount() == 2

    d.mic.setCurrentIndex(d.mic.findData("Headset Mikrofon"))
    d.speaker.setCurrentIndex(d.speaker.findData("Headset Kopfhörer"))
    d.mic_gain.setValue(6.0)
    d._accept()
    out = d.result_data
    assert out["audio"]["input_device"] == "Headset Mikrofon" and out["audio"]["output_device"] == "Headset Kopfhörer"
    assert out["audio"]["mic_gain_db"] == 6.0
    assert [l["name"] for l in out["line"]] == ["Kamera", "Technik"]


def test_settings_reject_a_key_used_twice_missing_lines_and_empty_names(app, tmp_path, monkeypatch):
    monkeypatch.setenv("INTERCOM_CLIENT_PASSWORD", "pw")
    d = dialog(tmp_path)
    d.pw.setText("x")
    d.table.cellWidget(1, 2).setKeySequence(QKeySequence("F1"))  # Regie's talk key again
    d._accept()
    assert d.result_data is None and "same key" in d.error.text()

    d = dialog(tmp_path)
    d.pw.setText("x")
    d.table.item(0, 0).setText("  ")
    d._accept()
    assert d.result_data is None and "Namen" in d.error.text()

    d = dialog(tmp_path)
    d.pw.setText("x")
    d.table.setRowCount(0)
    d._accept()
    assert d.result_data is None and "Leitung" in d.error.text()


def test_settings_for_a_first_start_need_server_and_user(app, tmp_path):
    d = dialog(tmp_path, with_cfg=False)
    assert d.table.rowCount() == 0
    d._accept()
    assert d.result_data is None and "Server" in d.error.text()


def test_settings_dialog_never_grows_beyond_the_screen_and_keeps_its_buttons_reachable(app, tmp_path):
    from PySide6.QtWidgets import QApplication, QDialogButtonBox, QScrollArea

    d = dialog(tmp_path)
    screen = QApplication.primaryScreen().availableGeometry()
    assert d.height() <= screen.height() and d.width() <= screen.width()
    scroll = d.findChild(QScrollArea)
    buttons = d.findChild(QDialogButtonBox)
    assert scroll is not None and not scroll.isAncestorOf(buttons)  # the buttons are outside the scrolling part

    # many lines make the content taller still: the dialog stays within the screen, the content scrolls
    for i in range(30):
        d._add_row(f"Leitung {i}", "9", str(i), None, None)
    d2 = dialog(tmp_path)
    assert d2.height() <= screen.height()


def test_after_disconnecting_the_cards_show_disconnected_not_the_last_state(app, tmp_path):
    cfg = tmp_path / "client.toml"
    write_cfg(cfg)
    win = MainWindow(tmp_path, cfg, enable_hotkeys=False, autostart=False)
    try:
        win.connect_now()
        assert pump(app, until=lambda: win.cards["Regie"].pill.text() == "Verbunden")
        win.btn_mute_all.click()
        win.cards["Regie"].talk.pressed.emit()
        win.toggle_connection()  # "Trennen"
        pump(app, 0.4)
        assert win.engine is None and win.btn_connect.text() == "Verbinden"
        for card in win.cards.values():
            assert card.pill.text() == "Getrennt" and card.since.text() == ""
            assert card.people.isHidden() and card.note.isHidden()
            assert not card.talk.isEnabled() and not card.latch.isEnabled() and not card.listen.isEnabled()
            assert card.connect_btn.text() == "Verbinden" and not card.connect_btn.isEnabled()
        assert not win.btn_mute_all.isChecked()
        win.toggle_connection()  # and connecting again brings the live state back
        assert pump(app, until=lambda: win.cards["Regie"].pill.text() == "Verbunden")
        assert win.cards["Regie"].talk.isEnabled()
    finally:
        win.close()


def test_the_button_on_a_card_disconnects_and_connects_just_that_line(app, tmp_path):
    cfg = tmp_path / "client.toml"
    write_cfg(cfg)
    win = MainWindow(tmp_path, cfg, enable_hotkeys=False, autostart=False)
    try:
        win.connect_now()
        assert pump(app, until=lambda: win.cards["Regie"].pill.text() == "Verbunden"
                    and win.cards["Technik"].pill.text() == "Verbunden")
        regie, technik = win.cards["Regie"], win.cards["Technik"]
        assert regie.connect_btn.text() == "Trennen" and regie.connect_btn.isEnabled()

        regie.connect_btn.click()
        assert pump(app, until=lambda: regie.pill.text() == "Getrennt")
        assert regie.connect_btn.text() == "Verbinden" and regie.connect_btn.isEnabled()
        assert not regie.talk.isEnabled() and not regie.latch.isEnabled()
        assert technik.pill.text() == "Verbunden" and technik.talk.isEnabled()  # the other line is untouched
        assert win.engine is not None and win.btn_connect.text() == "Trennen"

        regie.connect_btn.click()
        assert pump(app, until=lambda: regie.pill.text() == "Verbunden")
        assert regie.talk.isEnabled() and regie.connect_btn.text() == "Trennen"
    finally:
        win.close()
