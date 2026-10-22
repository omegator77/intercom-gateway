"""Main window: one card per line with a talk button, latch, volume and the people on the line."""
from __future__ import annotations

import logging
from pathlib import Path

import threading

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton,
                               QScrollArea, QSlider, QToolButton, QVBoxLayout, QWidget)

from gateway import credentials, tomlio
from gateway.ui.style import muted
from gateway.ui.window import STATE_TEXT, Meter, db_text, fmt_duration, people_html

from ..config import ClientCfg, ConfigError, load_client_config
from ..engine import ClientEngine
from ..hub import make_hub
from .settings import NOISE_LEVELS, ClientSettingsDialog

log = logging.getLogger("client.ui")

BTN = "border:none; border-radius:6px; padding:0 14px; font-weight:700; min-height:38px;"
TALK_IDLE = (f"QPushButton {{{BTN} background:#2f6fed; color:white;}} QPushButton:hover {{background:#4a82f0;}} "
             "QPushButton:disabled {background:#9aa0a6; color:#e8eaed;}")
TALK_ON = f"QPushButton {{{BTN} background:#d0472f; color:white;}}"
LATCH_IDLE = (f"QPushButton {{{BTN} background:#5f6368; color:white;}} QPushButton:hover {{background:#74797e;}} "
              "QPushButton:disabled {background:#9aa0a6; color:#e8eaed;}")
LATCH_ON = f"QPushButton {{{BTN} background:#c98a00; color:white;}}"
LISTEN_IDLE = LATCH_IDLE
LISTEN_ON = f"QPushButton {{{BTN} background:#b3261e; color:white;}}"
PLAIN_BTN = ("QPushButton {border:1px solid #8a8f94; border-radius:6px; padding:0 10px; min-height:34px;} "
             "QPushButton:hover {background:rgba(128, 128, 128, 0.25);} "
             "QPushButton:disabled {color:#9aa0a6; border-color:#c4c7c9;}")

BORDER_TALKING = "#d0472f"  # the whole card gets a coloured frame while you are transmitting into it
BORDER_LATCHED = "#c98a00"

# the gateway's wording, except that a line which is switched off is "Getrennt" (not "Aus")
COMPANION_TEXT = {
    "connecting": "Companion verbindet …",
    "connected": "Companion verbunden",
    "busy": "Companion: dort ist schon ein anderer Client verbunden (Web-App?)",
}


def companion_text(status: str) -> str:
    if status == "off":
        return ""
    if status.startswith("error"):
        return "Companion nicht erreichbar"
    return COMPANION_TEXT.get(status, "")


STATES = {**STATE_TEXT, "off": ("Getrennt", "#7a7f87"), "idle": ("Bereit", "#7a7f87")}


_KEY_SHORT = {"ctrl": "Strg", "shift": "⇧", "alt": "Alt", "win": "Win"}


def key_hint(spec: str | None) -> str:
    """'shift+f1' -> '  [⇧F1]': short, so that the buttons stay narrow."""
    if not spec:
        return ""
    out = ""
    for part in spec.split("+"):
        if part == "shift":
            out += _KEY_SHORT[part]  # the symbol needs no plus: "⇧F1"
        elif part in _KEY_SHORT:
            out += _KEY_SHORT[part] + "+"
        else:
            out += part.upper()
    return f"  [{out}]"


class LineCard(QFrame):
    """One line. Folded it is a single row with everything needed while working (talk, latch, mute, volume, level);
    unfolded it also shows who is on the line, the sending level and messages."""

    collapsedChanged = Signal(bool)

    def __init__(self, engine, name: str, ptt_key: str | None, latch_key: str | None, collapsed: bool = False) -> None:
        super().__init__()
        self.engine, self.name = engine, name
        self.collapsed = collapsed
        self._frame = ""
        self._keys = {"ptt": ptt_key, "latch": latch_key}
        self.setObjectName("LineCard")
        self.setFrameShape(QFrame.StyledPanel)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 6, 10, 6)
        outer.setSpacing(4)

        head = QHBoxLayout()
        head.setSpacing(6)
        self.arrow = QToolButton()
        self.arrow.setAutoRaise(True)
        self.arrow.setFocusPolicy(Qt.NoFocus)
        self.arrow.setToolTip("Details ein- oder ausklappen")
        self.title = QLabel(name)
        f = self.title.font()
        f.setPointSizeF(f.pointSizeF() * 1.15)
        f.setBold(True)
        self.title.setFont(f)
        self.title.setFixedWidth(100)  # fixed widths keep the buttons of all cards in the same columns
        self.title.setToolTip(name)
        self.pill = QLabel()
        self.pill.setAlignment(Qt.AlignCenter)
        self.pill.setFixedWidth(98)
        self.tag = QLabel()  # "Programm" / "Programm · gedimmt"
        self.tag.setVisible(False)
        self.count = QLabel()  # "2 weitere" - who is there, without unfolding
        muted(self.count)
        self.talk = QPushButton("Sprechen" + key_hint(ptt_key))
        self.talk.setToolTip("Gedrückt halten, um in diese Leitung zu sprechen")
        self.talk.setFixedWidth(128)
        self.latch = QPushButton("Dauer" + key_hint(latch_key))
        self.latch.setCheckable(True)
        self.latch.setToolTip("Dauer-Sprechen ein/aus")
        self.latch.setFixedWidth(112)
        self.listen = QPushButton("Stumm")
        self.listen.setCheckable(True)
        self.listen.setToolTip("Diese Leitung nicht hören")
        self.listen.setFixedWidth(72)
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 150)
        self.volume.setFixedWidth(100)
        self.volume_text = QLabel()
        self.volume_text.setMinimumWidth(38)
        self.out_meter = Meter()
        self.out_meter.setMinimumWidth(50)
        self.out_meter.setMaximumWidth(100)
        self.out_meter.setToolTip("Was in dieser Leitung ankommt")
        self.connect_btn = QPushButton("Trennen")
        self.connect_btn.setToolTip("Nur diese Leitung trennen bzw. wieder verbinden")
        self.connect_btn.setStyleSheet(PLAIN_BTN)
        self.connect_btn.setFixedWidth(72)
        for b in (self.talk, self.latch, self.listen, self.connect_btn):
            b.setFocusPolicy(Qt.NoFocus)  # the space bar must not trigger a button by accident
        slot = QWidget()  # "Programm" or "+2": one fixed slot, so nothing shifts
        slot.setFixedWidth(84)
        sl = QHBoxLayout(slot)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.addWidget(self.tag)
        sl.addWidget(self.count)
        sl.addStretch(1)
        for w in (self.arrow, self.title, self.pill, slot):
            head.addWidget(w)
        head.addSpacing(6)
        # the talk buttons keep their room even where they are hidden (the programme line is listen-only)
        self.talkbox = QWidget()
        self.talkbox.setFixedWidth(128 + 112 + 6)
        tb = QHBoxLayout(self.talkbox)
        tb.setContentsMargins(0, 0, 0, 0)
        tb.setSpacing(6)
        tb.addWidget(self.talk)
        tb.addWidget(self.latch)
        head.addWidget(self.talkbox)
        head.addWidget(self.listen)
        head.addSpacing(6)
        head.addWidget(self.volume)
        head.addWidget(self.volume_text)
        head.addWidget(self.out_meter, 1)
        head.addWidget(self.connect_btn)
        outer.addLayout(head)

        # the part that folds away
        self.details = QWidget()
        d = QVBoxLayout(self.details)
        d.setContentsMargins(26, 2, 0, 2)
        d.setSpacing(4)
        self.people = QLabel()
        self.people.setTextFormat(Qt.RichText)
        self.people.setWordWrap(True)
        d.addWidget(self.people)
        info = QHBoxLayout()
        self.since = QLabel()
        muted(self.since)
        self.send_label = QLabel("Senden")
        self.in_meter = Meter()
        info.addWidget(self.since)
        info.addStretch(1)
        info.addWidget(self.send_label)
        info.addWidget(self.in_meter, 1)
        d.addLayout(info)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setTextInteractionFlags(Qt.TextSelectableByMouse)
        d.addWidget(self.note)
        outer.addWidget(self.details)

        self.arrow.clicked.connect(lambda: self.set_collapsed(not self.collapsed))
        self.connect_btn.clicked.connect(lambda: engine.toggle_line(name))
        self.talk.pressed.connect(lambda: engine.ptt(name, True))
        self.talk.released.connect(lambda: engine.ptt(name, False))
        self.latch.clicked.connect(lambda: engine.toggle_latch(name))
        self.listen.clicked.connect(lambda: engine.set_listen_mute(name, self.listen.isChecked()))
        self.volume.valueChanged.connect(lambda v: engine.set_volume(name, v))
        self.talk.setStyleSheet(TALK_IDLE)
        self.latch.setStyleSheet(LATCH_IDLE)
        self.listen.setStyleSheet(LISTEN_IDLE)
        self._apply_collapsed()

    # -- folding

    def set_collapsed(self, collapsed: bool) -> None:
        if collapsed == self.collapsed:
            return
        self.collapsed = collapsed
        self._apply_collapsed()
        self.collapsedChanged.emit(collapsed)

    def _apply_collapsed(self) -> None:
        self.details.setVisible(not self.collapsed)
        self.arrow.setArrowType(Qt.RightArrow if self.collapsed else Qt.DownArrow)

    def _set_frame(self, color: str | None) -> None:
        style = f"QFrame#LineCard {{border:2px solid {color}; border-radius:8px;}}" if color else ""
        if style != self._frame:
            self._frame = style
            self.setStyleSheet(style)

    # -- what to show

    def show_off(self) -> None:
        """The client is disconnected altogether: nothing of the last state may stay on screen."""
        text, color = STATES["off"]
        self.pill.setText(text)
        self.pill.setStyleSheet(f"background:{color}; color:white; border-radius:9px; padding:2px 10px; font-weight:600;")
        self.pill.setToolTip("")
        self.since.setText("")
        self.count.setText("")
        self.tag.setVisible(False)
        self.people.setVisible(False)
        self.note.setVisible(False)
        self._set_frame(None)
        self.talk.setStyleSheet(TALK_IDLE)
        self.talk.setText("Sprechen" + key_hint(self.engine_key("ptt")))
        self.latch.setChecked(False)
        self.latch.setStyleSheet(LATCH_IDLE)
        self.listen.setChecked(False)
        self.listen.setStyleSheet(LISTEN_IDLE)
        self.in_meter.set_db(-120.0)
        self.out_meter.set_db(-120.0)
        self.connect_btn.setText("Verbinden")
        for w in (self.talk, self.latch, self.listen, self.connect_btn):
            w.setEnabled(False)

    def engine_key(self, which: str) -> str | None:
        return self._keys.get(which)

    def refresh(self, s: dict) -> None:
        self._keys = {"ptt": s["ptt_key"], "latch": s["latch_key"]}
        enabled = s.get("enabled", True)
        for w in (self.talk, self.latch, self.listen):
            w.setEnabled(enabled)
        self.connect_btn.setEnabled(True)
        self.connect_btn.setText("Trennen" if enabled else "Verbinden")
        text, color = STATES.get(s["state"], (s["state"], "#7a7f87"))
        self.pill.setText(text)
        self.pill.setStyleSheet(f"background:{color}; color:white; border-radius:9px; padding:2px 10px; font-weight:600;")
        self.pill.setToolTip(s["last_error"] or "")
        self.since.setText(f"verbunden seit {fmt_duration(s['uptime_s'])}" if s["state"] == "connected" else "")
        program = bool(s.get("program")) and enabled
        for w in (self.talk, self.latch, self.send_label, self.in_meter):
            w.setVisible(not program)  # the programme line is listen-only: no talk buttons there
        self.tag.setVisible(program)
        self.count.setVisible(not program)  # the slot shows the programme tag OR how many others are there
        if program:
            dimmed = bool(s.get("dimmed"))
            self.tag.setText("↓ gedimmt" if dimmed else "Programm")
            self.tag.setToolTip("Programm-Leitung" + (": wird gerade abgesenkt" if dimmed else ""))
            self.tag.setStyleSheet("background:#c98a00; color:white; border-radius:9px; padding:2px 10px; font-weight:600;"
                                   if dimmed else "background:#3b6ea5; color:white; border-radius:9px; padding:2px 10px;")
        connected = s["state"] == "connected"
        people = people_html(s["participants"]) if connected else ""
        self.people.setText(people)
        self.people.setVisible(bool(people))
        others = s["participants"] if connected else None
        self.count.setText("" if others is None else ("niemand sonst" if not others else f"+{len(others)}"))
        self.count.setToolTip("Wer noch in der Leitung ist: aufklappen" if others else "")

        held, latched = bool(s["held"] and s["talking"]), bool(s["latched"] and s["talking"])
        self.talk.setStyleSheet(TALK_ON if held else TALK_IDLE)
        self.talk.setText(("● Sendet" if held else "Sprechen") + key_hint(s["ptt_key"]))
        self.latch.setChecked(s["latched"])
        self.latch.setStyleSheet(LATCH_ON if s["latched"] else LATCH_IDLE)
        self.latch.setText(("● Dauer" if latched else "Dauer") + key_hint(s["latch_key"]))
        self.listen.setChecked(s["listen_mute"])
        self.listen.setStyleSheet(LISTEN_ON if s["listen_mute"] else LISTEN_IDLE)
        self.listen.setText("Stumm ●" if s["listen_mute"] else "Stumm")
        self._set_frame(BORDER_TALKING if held else (BORDER_LATCHED if latched else None))

        if not self.volume.isSliderDown() and self.volume.value() != s["volume_pct"]:
            self.volume.blockSignals(True)
            self.volume.setValue(s["volume_pct"])
            self.volume.blockSignals(False)
        self.volume_text.setText(f"{s['volume_pct']} %")
        self.in_meter.set_db(s["in_dbfs"] if s["talking"] else -120.0)
        self.out_meter.set_db(s["out_dbfs"])

        parts, bad = [], False
        a = s.get("audio") or {}
        if s["last_error"]:
            parts.append(s["last_error"])
            bad = True
        if connected and a.get("underruns"):
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
        self.resize(1100, 660)
        self.setMinimumWidth(1040)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        bar = QHBoxLayout()
        self.btn_connect = QPushButton("Verbinden")
        self.btn_mute_all = QPushButton("Alles stumm")
        self.btn_mute_all.setCheckable(True)
        self.btn_mute_all.setToolTip("Das Mikrofon in allen Leitungen aus (Sprechen und Dauer werden unterbrochen)")
        self.btn_settings = QPushButton("Einstellungen …")
        self.btn_collapse = QPushButton("Alle einklappen")
        self.btn_collapse.setToolTip("Alle Leitungen auf eine Zeile verkleinern bzw. wieder aufklappen")
        for b in (self.btn_connect, self.btn_mute_all, self.btn_settings, self.btn_collapse):
            b.setFocusPolicy(Qt.NoFocus)
        self.status = QLabel()
        muted(self.status)
        bar.addWidget(self.btn_connect)
        bar.addWidget(self.btn_mute_all)
        bar.addWidget(self.btn_settings)
        bar.addWidget(self.btn_collapse)
        bar.addStretch(1)
        bar.addWidget(QLabel("Mikrofon"))
        self.mic_meter = Meter()
        bar.addWidget(self.mic_meter)
        bar.addWidget(QLabel("Ausgang"))
        self.out_meter = Meter()
        bar.addWidget(self.out_meter)
        root.addLayout(bar)

        # the microphone, adjustable while the lines keep running
        mic = QHBoxLayout()
        mic.addWidget(QLabel("Mikrofon-Pegel"))
        self.mic_gain = QSlider(Qt.Horizontal)
        self.mic_gain.setRange(-20, 30)
        self.mic_gain.setFixedWidth(200)
        self.mic_gain_text = QLabel("+0 dB")
        self.mic_gain_text.setMinimumWidth(52)
        self.echo_box = QCheckBox("Echo-Unterdrückung")
        self.echo_box.setToolTip("Für Lautsprecher statt Kopfhörer. Mit Headset aus lassen.")
        self.noise_box = QComboBox()
        for label, value in NOISE_LEVELS:
            self.noise_box.addItem(f"Rauschen: {label}", value)
        self.noise_box.setToolTip("Rauschunterdrückung")
        self.agc_box = QCheckBox("Auto-Pegel")
        self.agc_box.setToolTip("Automatische Pegelregelung: regelt deine Sprache auf einen festen Ziel-Pegel, leise "
                                "Stimme wird angehoben, laute abgesenkt. In Sprechpausen bleibt die Verstärkung stehen "
                                "(Rauschen wird nicht hochgezogen), Spitzen werden sofort begrenzt. Der Regler links "
                                "wirkt davor.")
        for w in (self.mic_gain, self.echo_box, self.noise_box, self.agc_box):
            w.setFocusPolicy(Qt.NoFocus)
            w.setEnabled(False)
        mic.addWidget(self.mic_gain)
        mic.addWidget(self.mic_gain_text)
        mic.addSpacing(12)
        mic.addWidget(self.echo_box)
        mic.addWidget(self.noise_box)
        mic.addWidget(self.agc_box)
        mic.addStretch(1)
        root.addLayout(mic)
        root.addWidget(self.status)

        self.warning = QLabel()
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("color:#d0472f;")
        self.warning.setVisible(False)
        root.addWidget(self.warning)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.cards_host = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_host)
        self.cards_layout.setAlignment(Qt.AlignTop)
        self.scroll.setWidget(self.cards_host)
        root.addWidget(self.scroll, 1)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._persist)
        self._seen_lines: dict | None = None  # per line (enabled, volume) as last seen; a difference is saved
        self.mic_gain.valueChanged.connect(self._mic_gain_changed)
        self.echo_box.toggled.connect(self._processing_changed)
        self.noise_box.currentIndexChanged.connect(self._processing_changed)
        self.agc_box.toggled.connect(self._processing_changed)
        self.started.connect(self._started)
        self.btn_connect.clicked.connect(self.toggle_connection)
        self.btn_mute_all.clicked.connect(lambda: self.engine and self.engine.set_global_mute(self.btn_mute_all.isChecked()))
        self.btn_settings.clicked.connect(self.open_settings)
        self.btn_collapse.clicked.connect(self._toggle_all_collapsed)

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
        self._seen_lines = None
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

    def _card_folded(self, _collapsed: bool) -> None:
        self._update_collapse_button()
        self._save_timer.start(700)

    def _toggle_all_collapsed(self) -> None:
        """Fold every card to one row, or - when all are folded already - open them all."""
        fold = any(not c.collapsed for c in self.cards.values())
        for card in self.cards.values():
            card.set_collapsed(fold)
        self._update_collapse_button()

    def _update_collapse_button(self) -> None:
        all_folded = bool(self.cards) and all(c.collapsed for c in self.cards.values())
        self.btn_collapse.setText("Alle ausklappen" if all_folded else "Alle einklappen")
        self.btn_collapse.setEnabled(bool(self.cards))

    def _build_cards(self, cfg: ClientCfg, engine: ClientEngine) -> None:
        for card in self.cards.values():
            card.setParent(None)
            card.deleteLater()
        self.cards = {}
        for line in cfg.lines:
            card = LineCard(engine, line.name, line.ptt_key, line.latch_key, line.collapsed)
            card.collapsedChanged.connect(self._card_folded)
            self.cards[line.name] = card
            self.cards_layout.addWidget(card)
        self._update_collapse_button()

    # -- microphone: live, and remembered for the next start

    def _mic_gain_changed(self, db: int) -> None:
        self.mic_gain_text.setText(f"{db:+d} dB")
        if self.engine:
            self.engine.set_mic_gain_db(db)
            self._save_timer.start(700)

    def _processing_changed(self) -> None:
        if self.engine:
            self.engine.set_processing(self.echo_box.isChecked(), int(self.noise_box.currentData()),
                                       self.agc_box.isChecked())
            self._save_timer.start(700)

    def _persist(self) -> None:
        """Write what the user changed in the window (microphone, which lines are switched off, line volumes) into the
        config file, so the next start is the way this one was left. The rest of the file is kept."""
        eng = self.engine
        if eng is None or not self.config_path.exists():
            return
        try:
            raw = tomlio.load_raw(self.config_path)
            for entry in raw.get("line", []):
                name = entry.get("name")
                if name not in eng.controls:
                    continue
                if eng.line_enabled.get(name, True):
                    entry.pop("autoconnect", None)  # the default is not written
                else:
                    entry["autoconnect"] = False
                entry["volume"] = int(eng.controls[name].volume_pct)
                card = self.cards.get(name)
                if card is not None and card.collapsed:
                    entry["collapsed"] = True
                else:
                    entry.pop("collapsed", None)
            audio = dict(raw.get("audio") or {})
            audio["mic_gain_db"] = round(float(eng.mic_gain_db), 1)
            for key, value, default in (("echo_cancellation", eng.echo, False), ("noise_suppression", eng.noise, 0),
                                        ("auto_gain", eng.agc, False)):
                if value == default:
                    audio.pop(key, None)
                else:
                    audio[key] = value
            raw["audio"] = audio
            tomlio.save(self.config_path, raw)
        except OSError as e:
            log.warning("could not remember the settings: %s", e)

    def _sync_mic_controls(self, s: dict) -> None:
        """Show what the engine has (set on connect, and when something else changes it); never fight the user."""
        gain = int(round(s["mic_gain_db"]))
        if not self.mic_gain.isSliderDown() and self.mic_gain.value() != gain:
            self.mic_gain.blockSignals(True)
            self.mic_gain.setValue(gain)
            self.mic_gain.blockSignals(False)
        self.mic_gain_text.setText(f"{gain:+d} dB")
        gain = s.get("agc_gain_db")
        self.agc_box.setText("Auto-Pegel" if gain is None else f"Auto-Pegel ({gain:+.0f} dB)")
        for box, value in ((self.echo_box, s["echo"]), (self.agc_box, s["agc"])):
            if box.isChecked() != value:
                box.blockSignals(True)
                box.setChecked(value)
                box.blockSignals(False)
        idx = self.noise_box.findData(s["noise"])
        if idx >= 0 and self.noise_box.currentIndex() != idx:
            self.noise_box.blockSignals(True)
            self.noise_box.setCurrentIndex(idx)
            self.noise_box.blockSignals(False)
        for w in (self.mic_gain, self.echo_box, self.noise_box, self.agc_box):
            w.setEnabled(True)

    # -- refresh

    def _tick(self) -> None:
        eng = self.engine
        if eng is None:
            self.status.setText("Nicht verbunden" if self.config_path.exists() else "Noch nicht eingerichtet")
            self.mic_meter.set_db(-120.0)
            self.out_meter.set_db(-120.0)
            self.btn_mute_all.setChecked(False)
            self.btn_mute_all.setStyleSheet("")
            for w in (self.mic_gain, self.echo_box, self.noise_box, self.agc_box):
                w.setEnabled(False)
            for card in self.cards.values():
                card.show_off()
            return
        s = eng.snapshot()
        self._update_collapse_button()
        self._sync_mic_controls(s)
        seen = {l["name"]: (l["enabled"], l["volume_pct"]) for l in s["lines"]}
        if self._seen_lines is None:
            self._seen_lines = seen  # what the config said at the start: nothing to save
        elif seen != self._seen_lines:
            self._seen_lines = seen  # a line switched off or on, or a volume moved (by button, key or Companion)
            self._save_timer.start(700)
        self.mic_meter.set_db(s["mic_dbfs"])
        self.out_meter.set_db(s["out_dbfs"])
        self.btn_mute_all.setChecked(s["global_mute"])
        self.btn_mute_all.setStyleSheet(MUTE_ON if s["global_mute"] else "")
        hk = s["hotkeys"]
        key = f" ({s['global_mute_key'].upper()})" if s["global_mute_key"] else ""
        self.btn_mute_all.setText("Alles stumm" + key)
        proc = s["processing"]
        self.status.setText(f"{eng.cfg.username} @ {eng.cfg.server_url}"
                            + ("" if hk in ("ok", "off") else f"  ·  Globale Tasten nicht verfügbar: {hk}")
                            + ("  ·  Echo-/Rauschunterdrückung an" if proc == "on" else "")
                            + (f"  ·  {companion_text(s['companion'])}" if companion_text(s["companion"]) else ""))
        self._warn(proc if proc not in ("on", "off") else (self.engine.error or ""))
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
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._persist()  # a change made a moment ago must not be lost
        self.timer.stop()
        self.disconnect_now()
        super().closeEvent(event)
