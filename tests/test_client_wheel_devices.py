"""The mouse wheel does not turn controls by accident, "system default" means what the dialog says, and the window shows
what the echo / noise filter does."""
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtWidgets import (QApplication, QComboBox, QDoubleSpinBox, QLabel, QScrollArea, QSlider,  # noqa: E402
                               QVBoxLayout, QWidget)

import intercom_client.hub as hub_mod  # noqa: E402
from gateway import tomlio  # noqa: E402
from intercom_client.config import ClientAudio  # noqa: E402
from intercom_client.hub import AudioHub  # noqa: E402
from intercom_client.ui import wheel  # noqa: E402
from intercom_client.ui.settings import ClientSettingsDialog, DeviceCombo  # noqa: E402
from intercom_client.ui.window import MainWindow  # noqa: E402
from test_client_ui import _delete_widgets_after_each_test, app, fakes, pump, write_cfg  # noqa: E402,F401


def wheel_over(widget, dy):
    ev = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, dy), Qt.NoButton, Qt.NoModifier,
                     Qt.NoScrollPhase, False)
    QApplication.sendEvent(widget, ev)


class Page(QWidget):
    """The content of a scroll area: remembers the wheel events that were passed on to it."""

    def __init__(self):
        super().__init__()
        self.wheels = 0

    def wheelEvent(self, event):
        self.wheels += 1
        event.ignore()


def scrolling_window():
    host = Page()
    lay = QVBoxLayout(host)
    for i in range(40):
        lay.addWidget(QLabel(f"row {i}"))
    combo = DeviceCombo([("Mikro A", 2), ("Mikro B", 2), ("Mikro C", 2)], None)
    slider = QSlider(Qt.Horizontal)
    slider.setRange(0, 150)
    slider.setValue(100)
    spin = QDoubleSpinBox()
    spin.setRange(-40, 40)
    for w in (combo, slider, spin):
        lay.addWidget(w)
    area = QScrollArea()
    area.setWidget(host)
    area.resize(300, 200)
    area.show()
    return area, combo, slider, spin


def test_scrolling_over_a_combo_box_a_slider_or_a_spin_box_scrolls_instead_of_changing_them(app):
    wheel.install()
    area, combo, slider, spin = scrolling_window()
    app.processEvents()
    for w in (combo, slider, spin):
        w.clearFocus()  # a window gives its first control the focus; here nobody has clicked into any of them
    area.setFocus()
    app.processEvents()
    assert not (combo.hasFocus() or slider.hasFocus() or spin.hasFocus())
    page = area.widget()
    before = (combo.currentIndex(), slider.value(), spin.value())
    for w in (combo, slider, spin):
        wheel_over(w, -120)
        wheel_over(w, -120)
    app.processEvents()
    assert (combo.currentIndex(), slider.value(), spin.value()) == before  # nothing was turned
    assert page.wheels == 6  # every wheel step went on to the page around the controls, which scrolls


def test_a_control_you_clicked_into_still_takes_the_wheel(app):
    wheel.install()
    area, combo, slider, spin = scrolling_window()
    app.processEvents()
    combo.setFocus()
    app.processEvents()
    assert combo.hasFocus()
    wheel_over(combo, -120)
    assert combo.currentIndex() == 1  # one step on purpose


def test_the_wheel_guard_is_installed_once(app):
    wheel.install()
    first = wheel._guard
    wheel.install()
    assert wheel._guard is first


# ---------------------------------------------------------------- the system default

def test_system_default_is_the_device_windows_uses_and_the_dialog_names_it():
    class Sd:
        class default:
            device = (1, 11)  # what PortAudio's own default host API (MME) would pick

    apis = [{"name": "MME", "default_input_device": 1, "default_output_device": 11},
            {"name": "Windows WASAPI", "default_input_device": 52, "default_output_device": 43}]
    assert hub_mod._default_index(Sd, apis, "input") == 52 and hub_mod._default_index(Sd, apis, "output") == 43
    no_wasapi = [{"name": "MME", "default_input_device": 1, "default_output_device": 11}]
    assert hub_mod._default_index(Sd, no_wasapi, "input") == 1 and hub_mod._default_index(Sd, no_wasapi, "output") == 11
    unset = [{"name": "Windows WASAPI", "default_input_device": -1, "default_output_device": -1}]
    assert hub_mod._default_index(Sd, unset, "input") == 1  # nothing known: PortAudio's own default


def test_the_device_list_says_which_device_the_system_default_is(app):
    combo = DeviceCombo([("Headset", 2), ("Lautsprecher", 2)], None, "Lautsprecher")
    assert combo.itemText(0) == "Systemstandard (Lautsprecher)" and combo.value() == ""
    assert DeviceCombo([("Headset", 2)], None).itemText(0) == "Systemstandard"
    gone = DeviceCombo([("Headset", 2)], "USB Weg")  # a saved device that is not plugged in is shown as such, not replaced
    assert gone.value() == "USB Weg" and "nicht gefunden" in gone.currentText()


def test_the_settings_dialog_does_not_turn_a_device_when_the_wheel_scrolls_it(app, tmp_path):
    cfg = tmp_path / "client.toml"
    write_cfg(cfg)
    dlg = ClientSettingsDialog(cfg, device_lists=([("Mikro A", 2), ("Mikro B", 2)], [("Box A", 2), ("Box B", 2)]))
    dlg.show()
    app.processEvents()
    wheel_over(dlg.mic, -120)
    wheel_over(dlg.speaker, -120)
    assert dlg.mic.value() == "" and dlg.speaker.value() == ""  # still "system default"
    dlg.close()


# ---------------------------------------------------------------- what the filter does, shown

def test_the_hub_measures_the_microphone_before_and_after_the_noise_filter():
    from gateway.audio import SR

    hub = AudioHub(ClientAudio(backend="synthetic", noise_suppression=3))
    rng = np.random.default_rng(0)
    for _ in range(600):  # 6 s of steady noise at about -35 dBFS
        hub.process_mic((rng.standard_normal(480) * 0.0178).astype(np.float32))
    raw, proc, sent = hub.mic_raw_level.dbfs, hub.proc_level.dbfs, hub.mic_level.dbfs
    assert -40 < raw < -30
    assert proc < raw - 10  # the noise filter took more than 10 dB off steady noise
    assert sent == pytest.approx(proc, abs=0.5)  # no leveller: what is sent is what the filter left


def test_the_window_shows_the_filter_effect_only_while_the_filter_is_on(app, tmp_path):
    cfg = tmp_path / "client.toml"
    raw = {"server": {"url": "https://intercom.example", "username": "anna"},
           "audio": {"backend": "synthetic", "noise_suppression": 3},
           "line": [{"name": "Regie", "production_id": "4", "line_id": "1"}]}
    tomlio.save(cfg, raw)
    win = MainWindow(tmp_path, cfg, enable_hotkeys=False, autostart=False)
    try:
        win.connect_now()
        assert pump(app, until=lambda: win.engine is not None and win.engine.running and "Filter" in win.proc_text.text(),
                    timeout=10)
        assert "dBFS" in win.proc_text.text() and "dB)" in win.proc_text.text()
        win.noise_box.setCurrentIndex(win.noise_box.findData(0))  # filter off
        assert pump(app, until=lambda: win.proc_text.text() == "", timeout=5)
    finally:
        win.close()
