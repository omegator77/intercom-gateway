"""The window's side of updates: the offer, the question before anything is installed, and what is started."""
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QMessageBox  # noqa: E402

import intercom_client.ui.window as window_mod  # noqa: E402
from gateway import tomlio  # noqa: E402
from intercom_client import updates  # noqa: E402
from intercom_client.ui.window import MainWindow  # noqa: E402
from test_client_ui import _delete_widgets_after_each_test, app, fakes, pump, write_cfg  # noqa: E402,F401

INFO = updates.UpdateInfo("9.9.9", "Neu: alles", "intercom-client-9.9.9-setup.exe", "https://github.com/x", 10,
                          "https://github.com/s", "https://github.com/g", "https://github.com/p")


def window(tmp_path):
    cfg = tmp_path / "client.toml"
    write_cfg(cfg)
    return MainWindow(tmp_path, cfg, enable_hotkeys=False, autostart=False), cfg


def test_a_newer_version_is_offered_in_a_bar_and_later_hides_it(app, tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "latest_release", lambda current: INFO)
    win, _ = window(tmp_path)
    try:
        assert not win.update_bar.isVisible()
        win.check_updates(manual=False)
        assert pump(app, until=lambda: not win.update_bar.isHidden())
        assert "9.9.9" in win.update_bar.text.text() and win.update_bar.btn_install.isEnabled()
        win.update_bar.btn_later.click()
        assert win.update_bar.isHidden()
    finally:
        win.close()


def test_nothing_new_is_silent_on_its_own_and_said_when_asked_for_and_errors_likewise(app, tmp_path, monkeypatch):
    said = []
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda parent, title, text: said.append(text)))
    win, _ = window(tmp_path)
    try:
        monkeypatch.setattr(updates, "latest_release", lambda current: None)
        win.check_updates(manual=False)
        pump(app, 0.4)
        assert said == [] and win.update_bar.isHidden() and win.btn_update.isEnabled()
        win.check_updates(manual=True)
        assert pump(app, until=lambda: bool(said)) and "neueste" in said[0]
        assert win.btn_update.text() == "Update suchen" and win.btn_update.isEnabled()

        def broken(current):
            raise updates.UpdateError("GitHub ist nicht erreichbar: weg")

        said.clear()
        monkeypatch.setattr(updates, "latest_release", broken)
        win.check_updates(manual=False)
        pump(app, 0.4)
        assert said == []  # an automatic look-up that fails does not bother anybody
        win.check_updates(manual=True)
        assert pump(app, until=lambda: bool(said)) and "nicht erreichbar" in said[0]
    finally:
        win.close()


def test_the_installer_is_started_only_after_the_user_said_yes_and_the_client_leaves(app, tmp_path, monkeypatch):
    installer = tmp_path / "intercom-client-9.9.9-setup.exe"
    installer.write_bytes(b"MZ")
    monkeypatch.setattr(updates, "latest_release", lambda current: INFO)
    monkeypatch.setattr(updates, "download_update", lambda info, progress=None, **kw: (progress and progress(5, 10), installer)[1])
    started, quit_called, answers = [], [], [QMessageBox.No, QMessageBox.Yes]
    monkeypatch.setattr(window_mod.subprocess, "Popen", lambda cmd, **kw: started.append(cmd))
    monkeypatch.setattr(window_mod.QApplication, "quit", staticmethod(lambda: quit_called.append(True)))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: answers.pop(0)))
    win, _ = window(tmp_path)
    try:
        win.check_updates()
        assert pump(app, until=lambda: not win.update_bar.isHidden())
        win.update_bar.btn_install.click()
        assert pump(app, until=lambda: len(answers) == 1)  # asked once, answered "no"
        assert started == [] and quit_called == [] and win.update_bar.btn_install.isEnabled()
        win.update_bar.btn_install.click()
        assert pump(app, until=lambda: bool(started))
        assert started == [[str(installer)]] and quit_called == [True]  # nothing else is passed to the installer
    finally:
        win.close()


def test_a_failed_download_is_reported_and_nothing_is_started(app, tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "latest_release", lambda current: INFO)

    def bad(info, progress=None, **kw):
        raise updates.UpdateError("Die Signatur der Prüfsummen-Datei stimmt nicht. Das Update wird nicht installiert.")

    monkeypatch.setattr(updates, "download_update", bad)
    warned, started = [], []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda parent, title, text: warned.append(text)))
    monkeypatch.setattr(window_mod.subprocess, "Popen", lambda cmd, **kw: started.append(cmd))
    win, _ = window(tmp_path)
    try:
        win.check_updates()
        assert pump(app, until=lambda: not win.update_bar.isHidden())
        win.update_bar.btn_install.click()
        assert pump(app, until=lambda: bool(warned)) and "Signatur" in warned[0]
        assert started == [] and win.update_bar.btn_install.isEnabled()  # it can be tried again
    finally:
        win.close()


def test_the_automatic_look_up_can_be_switched_off_in_the_config(app, tmp_path):
    win, cfg = window(tmp_path)
    try:
        assert win._updates_enabled()
        raw = tomlio.load_raw(cfg)
        raw["updates"] = {"check": False}
        tomlio.save(cfg, raw)
        assert not win._updates_enabled()
    finally:
        win.close()


def test_the_settings_dialog_switches_the_automatic_look_up_off_and_on(app, tmp_path):
    from intercom_client.ui.settings import ClientSettingsDialog

    cfg = tmp_path / "client.toml"
    write_cfg(cfg)
    dlg = ClientSettingsDialog(cfg, device_lists=([], []))
    try:
        assert dlg.update_check.isChecked() and "updates" not in dlg._build()
        dlg.update_check.setChecked(False)
        assert dlg._build()["updates"] == {"check": False}
    finally:
        dlg.close()
        dlg.deleteLater()
        app.processEvents()
