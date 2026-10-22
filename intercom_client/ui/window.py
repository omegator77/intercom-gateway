"""Main window: one card per line with a talk button, latch, volume and the people on the line."""
from __future__ import annotations

import logging
from pathlib import Path

import threading

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton, QScrollArea, QSlider,
                               QVBoxLayout, QWidget)

from gateway import credentials, tomlio
from gateway.ui.style import muted
from gateway.ui.window import STATE_TEXT, Meter, db_text, fmt_duration, people_html

from ..config import ClientCfg, ConfigError, load_client_config
from ..engine import ClientEngine
from ..hub import make_hub
from .settings import ClientSettingsDialog

log = logging.getLogger("client.ui")

TALK_ON = "background:#d0472f; color:white; font-weight:700; border-radius:6px; padding:10px 18px;"
TALK_OFF = "font-weight:600; border-radius:6px; padding:10px 18px;"
LATCH_ON = "background:#c98a00; color:white; font-weight:700; border-radius:6px; padding:10px 14px;"
LATCH_OFF = "border-radius:6px; padding:10px 14px;"
MUTE_ON = "background:#7a7f87; color:white; border-radius:6px; padding:10px 14px;"

# the gateway's wording, except that a line which is switched off is "Getrennt" (not "Aus")
STATES = {**STATE_TEXT, "off": ("Getrennt", "#7a7f87"), "idle": ("Bereit", "#7a7f87")}


def key_hint(spec: str | None) -> str:
    return f"  [{spec.upper()}]" if spec else ""


class LineCard(QFrame):
    def __init__(self, engine: ClientEngine, name: str, ptt_key: str | None, latch_key: str | None) -> None:
        super().__init__()
        self.engine, self.name = engine, name
        self.setFrameShape(QFrame.StyledPanel)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        head = QHBoxLayout()
        self.title = QLabel(name)
        f = self.title.font()
        f.setPointSizeF(f.pointSizeF() * 1.25)
        f.setBold(True)
        self.title.setFont(f)
        self.pill = QLabel()
        self.pill.setAlignment(Qt.AlignCenter)
        self.since = QLabel()
        muted(self.since)
        self.connect_btn = QPushButton("Trennen")
        self.connect_btn.setFocusPolicy(Qt.NoFocus)
        self.connect_btn.setToolTip("Nur diese Leitung trennen bzw. wieder verbinden")
        head.addWidget(self.title)
        head.addWidget(self.pill)
        head.addStretch(1)
        head.addWidget(self.since)
        head.addWidget(self.connect_btn)
        outer.addLayout(head)

        self.people = QLabel()
        self.people.setTextFormat(Qt.RichText)
        self.people.setWordWrap(True)
        outer.addWidget(self.people)

        buttons = QHBoxLayout()
        self.talk = QPushButton("Sprechen" + key_hint(ptt_key))
        self.talk.setToolTip("Gedrückt halten, um in diese Leitung zu sprechen")
        self.talk.setMinimumWidth(190)
        self.latch = QPushButton("Dauer" + key_hint(latch_key))
        self.latch.setCheckable(True)
        self.latch.setToolTip("Dauer-Sprechen ein/aus")
        self.listen = QPushButton("Hören aus")
        self.listen.setCheckable(True)
        self.listen.setToolTip("Diese Leitung nicht hören")
        for b in (self.talk, self.latch, self.listen):
            b.setFocusPolicy(Qt.NoFocus)  # the space bar must not trigger a button by accident
            buttons.addWidget(b)
        buttons.addStretch(1)
        outer.addLayout(buttons)

        row = QHBoxLayout()
        row.addWidget(QLabel("Lautstärke"))
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 150)
        self.volume.setFixedWidth(220)
        self.volume_text = QLabel()
        self.volume_text.setMinimumWidth(44)
        row.addWidget(self.volume)
        row.addWidget(self.volume_text)
        row.addSpacing(16)
        row.addWidget(QLabel("Senden"))
        self.in_meter = Meter()
        row.addWidget(self.in_meter, 1)
        row.addSpacing(8)
        row.addWidget(QLabel("Hören"))
        self.out_meter = Meter()
        row.addWidget(self.out_meter, 1)
        outer.addLayout(row)

        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setTextInteractionFlags(Qt.TextSelectableByMouse)
        outer.addWidget(self.note)

        self.connect_btn.clicked.connect(lambda: engine.toggle_line(name))
        self.talk.pressed.connect(lambda: engine.ptt(name, True))
        self.talk.released.connect(lambda: engine.ptt(name, False))
        self.latch.clicked.connect(lambda: engine.toggle_latch(name))
        self.listen.clicked.connect(lambda: engine.set_listen_mute(name, self.listen.isChecked()))
        self.volume.valueChanged.connect(lambda v: engine.set_volume(name, v))

    def show_off(self) -> None:
        """The client is disconnected altogether: nothing of the last state may stay on screen."""
        text, color = STATES["off"]
        self.pill.setText(text)
        self.pill.setStyleSheet(f"background:{color}; color:white; border-radius:9px; padding:2px 10px; font-weight:600;")
        self.since.setText("")
        self.people.setVisible(False)
        self.note.setVisible(False)
        self.talk.setStyleSheet(TALK_OFF)
        self.talk.setText("Sprechen")
        self.latch.setChecked(False)
        self.latch.setStyleSheet(LATCH_OFF)
        self.in_meter.set_db(-120.0)
        self.out_meter.set_db(-120.0)
        self.connect_btn.setText("Verbinden")
        for w in (self.talk, self.latch, self.listen, self.connect_btn):
            w.setEnabled(False)

    def refresh(self, s: dict) -> None:
        enabled = s.get("enabled", True)
        for w in (self.talk, self.latch, self.listen):
            w.setEnabled(enabled)
        self.connect_btn.setEnabled(True)
        self.connect_btn.setText("Trennen" if enabled else "Verbinden")
        text, color = STATES.get(s["state"], (s["state"], "#7a7f87"))
        self.pill.setText(text)
        self.pill.setStyleSheet(f"background:{color}; color:white; border-radius:9px; padding:2px 10px; font-weight:600;")
        self.since.setText(f"verbunden seit {fmt_duration(s['uptime_s'])}" if s["state"] == "connected" else "")
        people = people_html(s["participants"]) if s["state"] == "connected" else ""
        self.people.setText(people)
        self.people.setVisible(bool(people))

        self.talk.setStyleSheet(TALK_ON if s["held"] and s["talking"] else TALK_OFF)
        self.talk.setText(("● Sendet" if s["held"] and s["talking"] else "Sprechen") + key_hint(s["ptt_key"]))
        self.latch.setChecked(s["latched"])
        self.latch.setStyleSheet(LATCH_ON if s["latched"] else LATCH_OFF)
        self.latch.setText(("● Dauer" if s["latched"] and s["talking"] else "Dauer") + key_hint(s["latch_key"]))
        self.listen.setChecked(s["listen_mute"])
        self.listen.setStyleSheet(MUTE_ON if s["listen_mute"] else "")

        if not self.volume.isSliderDown() and self.volume.value() != s["volume_pct"]:
            self.volume.blockSignals(True)
            self.volume.setValue(s["volume_pct"])
            self.volume.blockSignals(False)
        self.volume_text.setText(f"{s['volume_pct']} %")
        self.in_meter.set_db(s["in_dbfs"] if s["talking"] else -120.0)
        self.out_meter.set_db(s["out_dbfs"])

        parts, bad = [], False
        if s["last_error"]:
            parts.append(s["last_error"])
            bad = True
        a = s.get("audio") or {}
        if s["state"] == "connected" and a.get("underruns"):
            parts.append(f"{a['underruns']} Aussetzer beim Hören")
        self.note.setText("  ·  ".join(parts))
        self.note.setVisible(bool(parts))
        self.note.setStyleSheet("color:#d0472f;" if bad else "")


class MainWindow(QMainWindow):
    started = Signal()  # the engine has finished starting (from its helper thread to the GUI thread)

    def __init__(self, home: Path, config_path: Path, *, enable_hotkeys: bool = True, autostart: bool = True,
                 hub_factory=make_hub) -> None:
        super().__init__()
        self.home, self.config_path = home, config_path
        self._enable_hotkeys = enable_hotkeys
        self._hub_factory = hub_factory
        self.engine: ClientEngine | None = None
        self.cards: dict[str, LineCard] = {}
        self.setWindowTitle("Intercom-Client")
        self.resize(760, 560)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        bar = QHBoxLayout()
        self.btn_connect = QPushButton("Verbinden")
        self.btn_mute_all = QPushButton("Alles stumm")
        self.btn_mute_all.setCheckable(True)
        self.btn_mute_all.setToolTip("Das Mikrofon in allen Leitungen aus (Sprechen und Dauer werden unterbrochen)")
        self.btn_settings = QPushButton("Einstellungen …")
        for b in (self.btn_connect, self.btn_mute_all, self.btn_settings):
            b.setFocusPolicy(Qt.NoFocus)
        self.status = QLabel()
        muted(self.status)
        bar.addWidget(self.btn_connect)
        bar.addWidget(self.btn_mute_all)
        bar.addWidget(self.btn_settings)
        bar.addStretch(1)
        bar.addWidget(QLabel("Mikrofon"))
        self.mic_meter = Meter()
        bar.addWidget(self.mic_meter)
        bar.addWidget(QLabel("Ausgang"))
        self.out_meter = Meter()
        bar.addWidget(self.out_meter)
        root.addLayout(bar)
        root.addWidget(self.status)

        self.warning = QLabel()
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("color:#d0472f;")
        self.warning.setVisible(False)
        root.addWidget(self.warning)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.cards_host = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_host)
        self.cards_layout.setAlignment(Qt.AlignTop)
        self.scroll.setWidget(self.cards_host)
        root.addWidget(self.scroll, 1)

        self.started.connect(self._started)
        self.btn_connect.clicked.connect(self.toggle_connection)
        self.btn_mute_all.clicked.connect(lambda: self.engine and self.engine.set_global_mute(self.btn_mute_all.isChecked()))
        self.btn_settings.clicked.connect(self.open_settings)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(100)
        if autostart:
            QTimer.singleShot(0, self._autostart)

    # -- connection

    def _autostart(self) -> None:
        if self.config_path.exists():
            self.connect_now()
        else:
            self.open_settings()

    def _load(self) -> ClientCfg | None:
        try:
            return load_client_config(self.config_path)
        except ConfigError as e:
            self._warn(str(e))
            return None

    def _warn(self, text: str) -> None:
        self.warning.setText(text)
        self.warning.setVisible(bool(text))

    def connect_now(self) -> None:
        self.disconnect_now()
        cfg = self._load()
        if cfg is None:
            return
        self._warn("")
        engine = self.engine = ClientEngine(cfg, enable_hotkeys=self._enable_hotkeys, hub_factory=self._hub_factory)
        self._build_cards(cfg, engine)
        self.btn_connect.setText("Trennen")
        self.btn_connect.setEnabled(False)

        def start() -> None:
            engine.start()
            self.started.emit()

        threading.Thread(target=start, daemon=True).start()

    def _started(self) -> None:
        self.btn_connect.setEnabled(True)
        if self.engine and self.engine.error:
            self._warn(self.engine.error)
            self.btn_connect.setText("Verbinden")
            self.engine = None

    def disconnect_now(self) -> None:
        if self.engine:
            self.engine.stop()
            self.engine = None
        self.btn_connect.setText("Verbinden")
        self.btn_mute_all.setChecked(False)

    def toggle_connection(self) -> None:
        if self.engine:
            self.disconnect_now()
        else:
            self.connect_now()

    def _build_cards(self, cfg: ClientCfg, engine: ClientEngine) -> None:
        for card in self.cards.values():
            card.setParent(None)
            card.deleteLater()
        self.cards = {}
        for line in cfg.lines:
            card = LineCard(engine, line.name, line.ptt_key, line.latch_key)
            self.cards[line.name] = card
            self.cards_layout.addWidget(card)

    # -- refresh

    def _tick(self) -> None:
        eng = self.engine
        if eng is None:
            self.status.setText("Nicht verbunden" if self.config_path.exists() else "Noch nicht eingerichtet")
            self.mic_meter.set_db(-120.0)
            self.out_meter.set_db(-120.0)
            self.btn_mute_all.setChecked(False)
            self.btn_mute_all.setStyleSheet("")
            for card in self.cards.values():
                card.show_off()
            return
        s = eng.snapshot()
        self.mic_meter.set_db(s["mic_dbfs"])
        self.out_meter.set_db(s["out_dbfs"])
        self.btn_mute_all.setChecked(s["global_mute"])
        self.btn_mute_all.setStyleSheet(MUTE_ON if s["global_mute"] else "")
        hk = s["hotkeys"]
        key = f" ({s['global_mute_key'].upper()})" if s["global_mute_key"] else ""
        self.btn_mute_all.setText("Alles stumm" + key)
        self.status.setText(f"{eng.cfg.username} @ {eng.cfg.server_url}"
                            + ("" if hk in ("ok", "off") else f"  ·  Globale Tasten nicht verfügbar: {hk}"))
        for line in s["lines"]:
            card = self.cards.get(line["name"])
            if card:
                card.refresh(line)

    # -- settings

    def open_settings(self) -> None:
        dlg = ClientSettingsDialog(self.config_path, self)
        if dlg.exec() != ClientSettingsDialog.Accepted or dlg.result_data is None:
            return
        try:
            tomlio.save(self.config_path, dlg.result_data)
            if dlg.new_password:
                url = dlg.result_data["server"]["url"]
                user = dlg.result_data["server"]["username"]
                try:
                    credentials.set_password(url, user, dlg.new_password)
                except Exception as e:
                    QMessageBox.warning(self, "Passwort", f"Das Passwort konnte nicht gespeichert werden:\n{e}")
        except OSError as e:
            QMessageBox.critical(self, "Speichern", f"Die Einstellungen konnten nicht gespeichert werden:\n{e}")
            return
        self.connect_now()  # new settings take effect by connecting again

    def closeEvent(self, event) -> None:
        self.timer.stop()
        self.disconnect_now()
        super().closeEvent(event)
