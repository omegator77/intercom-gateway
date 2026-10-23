"""Changing the settings while the window is connected: only what concerns the connection connects again."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QDialog  # noqa: E402

from gateway import tomlio  # noqa: E402
from intercom_client.ui.settings import ClientSettingsDialog  # noqa: E402
from intercom_client.ui.window import MainWindow  # noqa: E402
from test_client_ui import _delete_widgets_after_each_test, app, fakes, pump, write_cfg  # noqa: E402,F401


def accepting(edit):
    """A settings dialog that is 'used' by `edit(dialog)` and accepted."""
    class Accepting(ClientSettingsDialog):
        def exec(self):
            edit(self)
            self._accept()
            return QDialog.DialogCode.Accepted

    return Accepting


def connected_window(app, tmp_path):
    cfg = tmp_path / "client.toml"
    write_cfg(cfg)
    win = MainWindow(tmp_path, cfg, enable_hotkeys=False, autostart=False)
    win.connect_now()
    assert pump(app, until=lambda: win.engine is not None and win.cards["Regie"].pill.text() == "Verbunden"
                and win.cards["Technik"].pill.text() == "Verbunden")
    return win, cfg


def test_changing_dimming_or_the_microphone_in_the_settings_does_not_reconnect(app, tmp_path, monkeypatch):
    import intercom_client.ui.window as window_mod

    win, cfg = connected_window(app, tmp_path)
    try:
        engine = win.engine
        workers = dict(engine.workers)

        def edit(dlg):
            dlg.dim_amount.setValue(-20)
            dlg.mic_gain.setValue(5)

        monkeypatch.setattr(window_mod, "ClientSettingsDialog", accepting(edit))
        win.open_settings()
        pump(app, 0.3)
        assert win.engine is engine and engine.workers == workers  # the same engine, the same line workers
        assert tomlio.load_raw(cfg)["dim"] == {"amount_db": -20}
        assert engine.hub._dimmer.cfg.amount_db == -20 and engine.mic_gain_db == 5.0
        assert win.cards["Regie"].pill.text() == "Verbunden" and win.cards["Technik"].pill.text() == "Verbunden"
        assert not win.warning.isVisible()
    finally:
        win.close()


def test_a_key_change_and_a_new_line_in_the_settings_touch_only_what_they_must(app, tmp_path, monkeypatch):
    import intercom_client.ui.window as window_mod

    win, cfg = connected_window(app, tmp_path)
    try:
        engine = win.engine
        regie_worker = engine.workers["Regie"]
        raw = tomlio.load_raw(cfg)
        raw["line"][1]["ptt_key"] = "f7"
        raw["line"].append({"name": "Studio", "production_id": "5", "line_id": "3"})
        tomlio.save(cfg, raw)

        class FromFile(ClientSettingsDialog):  # the dialog accepts what the file says, like an edit that changed it
            def exec(self):
                self._accept()
                return QDialog.DialogCode.Accepted

        monkeypatch.setattr(window_mod, "ClientSettingsDialog", FromFile)
        win.open_settings()
        assert pump(app, until=lambda: "Studio" in win.cards and win.cards["Studio"].pill.text() == "Verbunden")
        assert win.engine is engine and engine.workers["Regie"] is regie_worker  # the other lines were not reconnected
        assert list(win.cards) == ["Regie", "Technik", "Studio"]
        assert "[F7]" in win.cards["Technik"].talk.text()
    finally:
        win.close()


def test_a_changed_login_connects_everything_again(app, tmp_path, monkeypatch):
    import intercom_client.ui.window as window_mod

    win, cfg = connected_window(app, tmp_path)
    try:
        old = win.engine

        def edit(dlg):
            dlg.user.setText("ben")

        monkeypatch.setattr(window_mod, "ClientSettingsDialog", accepting(edit))
        monkeypatch.setattr(window_mod.credentials, "set_password", lambda *a: None)
        win.open_settings()
        assert pump(app, until=lambda: win.engine is not None and win.engine is not old
                    and win.cards["Regie"].pill.text() == "Verbunden")
        assert win.engine.cfg.username == "ben"
    finally:
        win.close()
