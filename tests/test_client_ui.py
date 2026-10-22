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


# ---------------------------------------------------------------- processing and dimming settings

def test_settings_show_the_defaults_for_processing_and_dimming(app, tmp_path):
    d = dialog(tmp_path)
    assert d.preset.currentData() == "headset" and not d.echo.isChecked() and d.noise.currentData() == 0
    assert not d.agc.isChecked()
    assert d.dim_amount.value() == -14 and d.dim_addressed.isChecked() and d.dim_speaking.isChecked()
    assert d.dim_delay.currentData() == 1000
    d.pw.setText("x")
    d._accept()
    out = d.result_data
    assert not {"echo_cancellation", "noise_suppression", "auto_gain"} & set(out["audio"])  # defaults are not written
    assert "dim" not in out


def test_the_speaker_preset_switches_echo_and_noise_on_and_changes_make_it_a_custom_setting(app, tmp_path):
    d = dialog(tmp_path)
    d.pw.setText("x")
    d.preset.setCurrentIndex(d.preset.findData("speakers"))
    d._apply_preset()
    assert d.echo.isChecked() and d.noise.currentData() == 2 and not d.agc.isChecked()
    d._accept()
    audio = d.result_data["audio"]
    assert audio["echo_cancellation"] is True and audio["noise_suppression"] == 2 and "auto_gain" not in audio

    d = dialog(tmp_path)
    d.pw.setText("x")
    d.agc.setChecked(True)
    assert d.preset.currentData() == "custom"
    d.noise.setCurrentIndex(d.noise.findData(4))
    d._accept()
    assert d.result_data["audio"] == {**d.result_data["audio"], "noise_suppression": 4, "auto_gain": True}

    d.preset.setCurrentIndex(d.preset.findData("headset"))
    d._apply_preset()
    assert not d.echo.isChecked() and d.noise.currentData() == 0 and not d.agc.isChecked()


def test_settings_save_and_reload_the_dimming_and_processing_values(app, tmp_path):
    d = dialog(tmp_path)
    d.pw.setText("x")
    d.dim_amount.setValue(-20)
    d.dim_addressed.setChecked(False)
    d.dim_delay.setCurrentIndex(d.dim_delay.findData(250))
    d.echo.setChecked(True)
    d._accept()
    out = d.result_data
    assert out["dim"] == {"amount_db": -20, "when_addressed": False, "delay_ms": 250}
    tomlio.save(tmp_path / "client.toml", out)

    again = dialog(tmp_path, with_cfg=False)
    assert again.dim_amount.value() == -20 and not again.dim_addressed.isChecked() and again.dim_speaking.isChecked()
    assert again.dim_delay.currentData() == 250 and again.echo.isChecked()

    again.dim_amount.setValue(-14)
    again.dim_addressed.setChecked(True)
    again.dim_delay.setCurrentIndex(again.dim_delay.findData(1000))
    again.echo.setChecked(False)
    again.pw.setText("x")
    again._accept()
    assert "dim" not in again.result_data and "echo_cancellation" not in again.result_data["audio"]


# ---------------------------------------------------------------- Stream Deck / Companion

def test_settings_save_the_companion_address_and_complete_a_bare_host_and_port(app, tmp_path):
    d = dialog(tmp_path)
    assert d.companion.text() == ""
    d.pw.setText("x")
    d._accept()
    assert "companion" not in d.result_data  # off: nothing in the file

    d = dialog(tmp_path)
    d.pw.setText("x")
    d.companion.setText("192.168.1.20:12345")
    d._accept()
    assert d.result_data["companion"] == {"url": "ws://192.168.1.20:12345"}
    tomlio.save(tmp_path / "client.toml", d.result_data)
    assert dialog(tmp_path, with_cfg=False).companion.text() == "ws://192.168.1.20:12345"

    d = dialog(tmp_path)
    d.pw.setText("x")
    d.companion.setText("http://x:1")
    d._accept()
    assert d.result_data is None and "ws://" in d.error.text()


def test_the_window_tells_how_the_companion_link_is_doing():
    from intercom_client.ui.window import companion_text

    assert companion_text("off") == ""
    assert companion_text("connected") == "Companion verbunden"
    assert "schon ein anderer Client" in companion_text("busy")
    assert companion_text("error: ConnectionRefusedError: x") == "Companion nicht erreichbar"
    assert companion_text("connecting").startswith("Companion verbindet")


# ---------------------------------------------------------------- microphone controls in the main window, listen-only programme tile

def test_the_main_window_changes_and_remembers_the_microphone_without_reconnecting(app, tmp_path):
    cfg = tmp_path / "client.toml"
    write_cfg(cfg)
    win = MainWindow(tmp_path, cfg, enable_hotkeys=False, autostart=False)
    try:
        assert not win.mic_gain.isEnabled() and not win.agc_box.isEnabled()  # nothing to adjust before connecting
        win.connect_now()
        assert pump(app, until=lambda: win.cards["Regie"].pill.text() == "Verbunden" and win.mic_gain.isEnabled())
        eng = win.engine
        workers = dict(eng.workers)
        assert win.mic_gain.value() == 0 and not win.agc_box.isChecked() and win.noise_box.currentData() == 0

        win.mic_gain.setValue(9)
        win.agc_box.setChecked(True)
        win.echo_box.setChecked(True)
        win.noise_box.setCurrentIndex(win.noise_box.findData(3))
        assert eng.mic_gain_db == 9.0 and eng.agc and eng.echo and eng.noise == 3
        assert eng.hub._proc is not None and eng.hub.mic_gain_db == 9.0  # live in the audio
        assert win.mic_gain_text.text() == "+9 dB"
        assert dict(eng.workers) == workers  # no line was reconnected
        assert pump(app, until=lambda: tomlio.load_raw(cfg).get("audio", {}).get("mic_gain_db") == 9.0, timeout=3)
        audio = tomlio.load_raw(cfg)["audio"]
        assert audio["auto_gain"] is True and audio["echo_cancellation"] is True and audio["noise_suppression"] == 3
        assert tomlio.load_raw(cfg)["line"][0]["ptt_key"] == "f1"  # the rest of the file is untouched

        win.agc_box.setChecked(False)
        win.echo_box.setChecked(False)
        win.noise_box.setCurrentIndex(win.noise_box.findData(0))
        win._persist_audio()
        audio = tomlio.load_raw(cfg)["audio"]
        assert not {"auto_gain", "echo_cancellation", "noise_suppression"} & set(audio)  # defaults are not written
    finally:
        win.close()

    # the next start uses what was set
    from intercom_client.config import load_client_config

    assert load_client_config(cfg).audio.mic_gain_db == 9.0


def test_a_change_made_just_before_closing_is_still_saved(app, tmp_path):
    cfg = tmp_path / "client.toml"
    write_cfg(cfg)
    win = MainWindow(tmp_path, cfg, enable_hotkeys=False, autostart=False)
    win.connect_now()
    assert pump(app, until=lambda: win.mic_gain.isEnabled())
    win.mic_gain.setValue(-5)
    win.close()  # before the save timer fired
    assert tomlio.load_raw(cfg)["audio"]["mic_gain_db"] == -5.0


def test_the_programme_tile_has_no_talk_buttons(app, tmp_path, monkeypatch):
    class ProgramWorker(FakeWorker):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.program_line = self.cfg.name == "Technik"

        def status(self):
            return {**super().status(), "program_line": self.program_line}

    monkeypatch.setattr(engine_mod, "LineWorker", ProgramWorker)
    cfg = tmp_path / "client.toml"
    write_cfg(cfg)
    win = MainWindow(tmp_path, cfg, enable_hotkeys=False, autostart=False)
    try:
        win.connect_now()
        assert pump(app, until=lambda: "Technik" in win.cards and win.cards["Technik"].tag.text().startswith("Programm"),
                    timeout=10)
        technik, regie = win.cards["Technik"], win.cards["Regie"]
        assert technik.talk.isHidden() and technik.latch.isHidden() and technik.in_meter.isHidden()
        assert not technik.listen.isHidden() and not technik.volume.isHidden()  # listening is still adjustable
        assert not regie.talk.isHidden() and not regie.latch.isHidden()
        win.engine.ptt("Technik", True)
        assert not win.engine.controls["Technik"].talk.held
    finally:
        win.close()
