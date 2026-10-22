"""Settings: server and account, the lines to join (with their keys), audio devices and the global keys.
Writes the same TOML file the client reads; the password goes to the Windows Credential Manager, never the file."""
from __future__ import annotations

import asyncio
import copy
import threading
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QHeaderView,
                               QKeySequenceEdit, QLabel, QLineEdit, QPushButton, QScrollArea, QSpinBox, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from gateway import credentials, tomlio
from gateway.auth import LoginError
from gateway.lines import AvailableLine, LinesError, fetch_available_lines
from gateway.ui import devices
from gateway.ui.style import muted

from ..config import ClientDim, ConfigError, parse_client_config
from ..hotkeys import normalize_key

SYSTEM_DEFAULT = "Systemstandard"
NOISE_LEVELS = [("Aus", 0), ("Niedrig", 1), ("Mittel", 2), ("Hoch", 3), ("Sehr hoch", 4)]
DIM_DELAYS = [("Sofort", 0), ("0,25 s", 250), ("0,5 s", 500), ("1 s (Standard)", 1000), ("2 s", 2000)]
COL_NAME, COL_LINE, COL_PTT, COL_LATCH = range(4)


def key_spec(sequence: QKeySequence) -> str | None:
    """Qt's 'Ctrl+Shift+F1' -> our 'ctrl+shift+f1' (None for an empty field)."""
    text = sequence.toString(QKeySequence.PortableText)
    if not text:
        return None
    return "+".join(normalize_key(p) for p in text.split(",")[0].split("+") if p)


class KeyEdit(QKeySequenceEdit):
    """One key combination; clearing the field removes the key."""

    def __init__(self, spec: str | None = None) -> None:
        super().__init__()
        self.setMaximumSequenceLength(1)
        self.setClearButtonEnabled(True)
        if hasattr(self, "setPlaceholderText"):  # Qt 6.4+
            self.setPlaceholderText("Taste drücken")
        if spec:
            self.setKeySequence(QKeySequence("+".join(p.capitalize() if len(p) > 1 else p.upper()
                                                      for p in spec.split("+"))))

    def spec(self) -> str | None:
        return key_spec(self.keySequence())


class DeviceCombo(QComboBox):
    def __init__(self, names: list[tuple[str, int]], current: str | None) -> None:
        super().__init__()
        self.setMinimumWidth(220)
        self.addItem(SYSTEM_DEFAULT, "")
        for name, ch in names:
            self.addItem(f"{name}  ({ch} Kanäle)", name)
        if current and self.findData(current) < 0:  # configured device not present right now: keep it visible
            self.addItem(f"{current}  (nicht gefunden)", current)
        self.setCurrentIndex(max(0, self.findData(current or "")))

    def value(self) -> str:
        return self.currentData() or ""


class ClientSettingsDialog(QDialog):
    loginResult = Signal(str, bool)
    linesLoaded = Signal(object)  # list[AvailableLine]

    def __init__(self, config_path: Path, parent=None, *, device_lists=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Einstellungen")
        self.config_path = config_path
        self._raw = tomlio.load_raw(config_path) if config_path.exists() else {}
        raw = self._raw
        server, audio, hotkeys = raw.get("server") or {}, raw.get("audio") or {}, raw.get("hotkeys") or {}
        self.result_data: dict | None = None
        self.new_password: str | None = None
        self.available: list[AvailableLine] = []
        inputs, outputs = device_lists if device_lists is not None else devices.list_devices()

        # -- account
        box_srv = QGroupBox("Server und Konto")
        f = QFormLayout(box_srv)
        self.url = QLineEdit(server.get("url", ""))
        self.url.setPlaceholderText("https://intercom.example.org")
        self.user = QLineEdit(server.get("username", ""))
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.Password)
        stored = bool(server.get("url") and server.get("username")
                      and credentials.get_password(server["url"], server["username"]))
        self.pw.setPlaceholderText("gespeichert — leer lassen, um es zu behalten" if stored else "Passwort")
        self._stored_pw = stored
        self.btn_login = QPushButton("Anmelden und Leitungen laden")
        self.login_result = QLabel()
        self.login_result.setWordWrap(True)
        row = QHBoxLayout()
        row.addWidget(self.btn_login)
        row.addWidget(self.login_result, 1)
        f.addRow("Server", self.url)
        f.addRow("Benutzer", self.user)
        f.addRow("Passwort", self.pw)
        name_note = QLabel("Die anderen sehen dich unter dem Namen deines Kontos (im Web unter „User Settings“ änderbar).")
        name_note.setWordWrap(True)
        muted(name_note)
        f.addRow("", name_note)
        f.addRow("", row)

        # -- lines
        box_lines = QGroupBox("Leitungen")
        v = QVBoxLayout(box_lines)
        add_row = QHBoxLayout()
        self.line_pick = QComboBox()
        self.line_pick.setMinimumWidth(240)
        self.line_pick.addItem("Erst anmelden, dann Leitungen hinzufügen …", None)
        self.btn_add = QPushButton("Hinzufügen")
        add_row.addWidget(self.line_pick, 1)
        add_row.addWidget(self.btn_add)
        v.addLayout(add_row)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Name", "Leitung", "Sprechen-Taste", "Dauer-Taste"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        hdr.setSectionResizeMode(COL_LINE, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(COL_PTT, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(COL_LATCH, QHeaderView.ResizeToContents)
        self.table.setMinimumHeight(110)
        v.addWidget(self.table)
        ops = QHBoxLayout()
        self.btn_up, self.btn_down, self.btn_remove = QPushButton("↑"), QPushButton("↓"), QPushButton("Entfernen")
        for b in (self.btn_up, self.btn_down, self.btn_remove):
            ops.addWidget(b)
        ops.addStretch(1)
        hint = QLabel("Die Reihenfolge ist auch die Tastennummer am Stream Deck.")
        hint.setWordWrap(True)
        muted(hint)
        ops.addWidget(hint, 1)
        v.addLayout(ops)
        self._volumes: dict[str, int] = {}
        for line in raw.get("line", []):
            self._volumes[str(line.get("name", ""))] = int(line.get("volume", 100))
            self._add_row(line.get("name", ""), str(line.get("production_id", "")), str(line.get("line_id", "")),
                          line.get("ptt_key"), line.get("latch_key"))

        # -- audio
        box_audio = QGroupBox("Audio")
        fa = QFormLayout(box_audio)
        self.mic = DeviceCombo(inputs, audio.get("input_device"))
        self.speaker = DeviceCombo(outputs, audio.get("output_device"))
        self.mic_gain = QDoubleSpinBox()
        self.mic_gain.setRange(-40, 40)
        self.mic_gain.setSingleStep(1.0)
        self.mic_gain.setSuffix(" dB")
        self.mic_gain.setValue(float(audio.get("mic_gain_db", 0.0)))
        fa.addRow("Mikrofon", self.mic)
        fa.addRow("Ausgang (Kopfhörer)", self.speaker)
        fa.addRow("Mikrofon-Verstärkung", self.mic_gain)

        # -- microphone processing
        box_proc = QGroupBox("Mikrofon-Aufbereitung")
        fp = QFormLayout(box_proc)
        self.preset = QComboBox()
        self.preset.addItem("Headset (alles aus, beste Qualität)", "headset")
        self.preset.addItem("Lautsprecher (Echo + Rauschen)", "speakers")
        self.preset.addItem("Eigene Einstellung", "custom")
        self.echo = QCheckBox("Echo-Unterdrückung (nötig bei Lautsprechern)")
        self.noise = QComboBox()
        for label, value in NOISE_LEVELS:
            self.noise.addItem(label, value)
        self.agc = QCheckBox("Automatische Pegelregelung (Auto-Pegel)")
        self.echo.setChecked(bool(audio.get("echo_cancellation", False)))
        self.noise.setCurrentIndex(max(0, self.noise.findData(int(audio.get("noise_suppression", 0)))))
        self.agc.setChecked(bool(audio.get("auto_gain", False)))
        fp.addRow("Voreinstellung", self.preset)
        fp.addRow("", self.echo)
        fp.addRow("Rauschunterdrückung", self.noise)
        fp.addRow("", self.agc)
        self.agc_target = QSpinBox()
        self.agc_target.setRange(-35, -10)
        self.agc_target.setSuffix(" dBFS")
        self.agc_target.setValue(int(audio.get("agc_target_dbfs", -20)))
        self.agc_target.setToolTip("Pegel, auf den die Sprache geregelt wird (Standard −20 dBFS)")
        self.agc_max = QSpinBox()
        self.agc_max.setRange(0, 40)
        self.agc_max.setSuffix(" dB")
        self.agc_max.setValue(int(audio.get("agc_max_gain_db", 25)))
        self.agc_max.setToolTip("Mehr als das hebt der Auto-Pegel nie an (Standard 25 dB)")
        fp.addRow("Auto-Pegel Ziel", self.agc_target)
        fp.addRow("Auto-Pegel max. Verstärkung", self.agc_max)
        proc_note = QLabel("Mit Headset alles aus lassen. Bei Lautsprechern die Echo-Unterdrückung einschalten, sonst "
                           "gibt es Rückkopplung.")
        proc_note.setWordWrap(True)
        muted(proc_note)
        fp.addRow("", proc_note)
        self._sync_preset()
        self.preset.activated.connect(self._apply_preset)
        for w in (self.echo, self.agc):
            w.toggled.connect(self._sync_preset)
        self.noise.currentIndexChanged.connect(self._sync_preset)

        # -- dimming of the programme line
        dim = raw.get("dim") or {}
        box_dim = QGroupBox("Programm-Leitung dimmen")
        fd = QFormLayout(box_dim)
        self.dim_amount = QSpinBox()
        self.dim_amount.setRange(-40, 0)
        self.dim_amount.setSuffix(" dB")
        self.dim_amount.setSpecialValueText("aus")
        self.dim_amount.setValue(int(dim.get("amount_db", -14)))
        self.dim_addressed = QCheckBox("wenn mich jemand anspricht")
        self.dim_addressed.setChecked(bool(dim.get("when_addressed", True)))
        self.dim_speaking = QCheckBox("wenn ich spreche")
        self.dim_speaking.setChecked(bool(dim.get("while_speaking", True)))
        self.dim_delay = QComboBox()
        for label, ms in DIM_DELAYS:
            self.dim_delay.addItem(label, ms)
        self.dim_delay.setCurrentIndex(max(0, self.dim_delay.findData(int(dim.get("delay_ms", 1000)))))
        fd.addRow("Absenkung", self.dim_amount)
        fd.addRow("Dimmen", self.dim_addressed)
        fd.addRow("", self.dim_speaking)
        fd.addRow("Reaktionszeit", self.dim_delay)
        dim_note = QLabel("Welche Leitung die Programm-Leitung ist, erkennt der Client an der Markierung am Server.")
        dim_note.setWordWrap(True)
        muted(dim_note)
        fd.addRow("", dim_note)

        # -- keys
        box_keys = QGroupBox("Globale Tasten")
        fk = QFormLayout(box_keys)
        self.mute_all_key = KeyEdit(hotkeys.get("global_mute"))
        fk.addRow("Alles stumm", self.mute_all_key)

        # -- Stream Deck
        box_deck = QGroupBox("Stream Deck (Bitfocus Companion)")
        fs = QFormLayout(box_deck)
        self.companion = QLineEdit((raw.get("companion") or {}).get("url", ""))
        self.companion.setPlaceholderText("ws://127.0.0.1:12345   (leer = aus)")
        fs.addRow("Adresse des Moduls", self.companion)
        deck_note = QLabel("Das Companion-Modul „Eyevinn: Open Intercom“ nimmt nur einen Client an: entweder die "
                           "Web-App oder dieser Client. Die Tasten gelten für die Leitungen in der Reihenfolge der "
                           "Liste oben (1 = erste Leitung).")
        deck_note.setWordWrap(True)
        muted(deck_note)
        fs.addRow("", deck_note)

        self.error = QLabel()
        self.error.setStyleSheet("color:#d0472f;")
        self.error.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Speichern und verbinden")
        buttons.button(QDialogButtonBox.Cancel).setText("Abbrechen")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        # The sections scroll, the buttons stay put: on a small or scaled-up screen the dialog is taller than
        # the screen, and the buttons must never be out of reach.
        content = QWidget()
        lay = QVBoxLayout(content)
        for w in (box_srv, box_lines, box_audio, box_proc, box_dim, box_keys, box_deck):
            lay.addWidget(w)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.addWidget(self.scroll, 1)
        outer.addWidget(self.error)
        outer.addWidget(buttons)
        screen = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        self.setMinimumWidth(min(720, screen.width() - 40))
        wanted = content.sizeHint().height() + buttons.sizeHint().height() + 60
        self.resize(min(760, screen.width() - 40), min(wanted, int(screen.height() * 0.85)))

        self.btn_login.clicked.connect(self._login)
        self.loginResult.connect(self._show_login)
        self.linesLoaded.connect(self._show_lines)
        self.btn_add.clicked.connect(self._add_picked)
        self.btn_remove.clicked.connect(self._remove)
        self.btn_up.clicked.connect(lambda: self._move(-1))
        self.btn_down.clicked.connect(lambda: self._move(1))

    # -- microphone processing presets

    PRESETS = {"headset": (False, 0, False), "speakers": (True, 2, False)}

    def _current_processing(self) -> tuple[bool, int, bool]:
        return self.echo.isChecked(), int(self.noise.currentData()), self.agc.isChecked()

    def _sync_preset(self) -> None:
        """The preset box shows which preset the three settings match, or 'own setting'."""
        cur = self._current_processing()
        key = next((k for k, v in self.PRESETS.items() if v == cur), "custom")
        self.preset.blockSignals(True)
        self.preset.setCurrentIndex(self.preset.findData(key))
        self.preset.blockSignals(False)

    def _apply_preset(self) -> None:
        values = self.PRESETS.get(self.preset.currentData())
        if values is None:
            return
        echo, noise, agc = values
        for w, v in ((self.echo, echo), (self.agc, agc)):
            w.blockSignals(True)
            w.setChecked(v)
            w.blockSignals(False)
        self.noise.blockSignals(True)
        self.noise.setCurrentIndex(self.noise.findData(noise))
        self.noise.blockSignals(False)

    # -- the lines table

    def _add_row(self, name: str, prod: str, line: str, ptt: str | None, latch: str | None) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, COL_NAME, QTableWidgetItem(name))
        ident = QTableWidgetItem(f"{prod} / {line}")
        ident.setFlags(ident.flags() & ~Qt.ItemIsEditable)
        ident.setData(Qt.UserRole, (prod, line))
        self.table.setItem(r, COL_LINE, ident)
        self.table.setCellWidget(r, COL_PTT, KeyEdit(ptt))
        self.table.setCellWidget(r, COL_LATCH, KeyEdit(latch))

    def _existing_ids(self) -> set[tuple[str, str]]:
        return {self.table.item(r, COL_LINE).data(Qt.UserRole) for r in range(self.table.rowCount())}

    def _add_picked(self) -> None:
        data = self.line_pick.currentData()
        if not data:
            return
        prod, line, name = data
        if (prod, line) in self._existing_ids():
            self.error.setText("Diese Leitung ist schon in der Liste.")
            return
        self.error.setText("")
        self._add_row(name, prod, line, None, None)

    def _remove(self) -> None:
        r = self.table.currentRow()
        if r >= 0:
            self.table.removeRow(r)

    def _move(self, delta: int) -> None:
        r = self.table.currentRow()
        t = r + delta
        if r < 0 or not 0 <= t < self.table.rowCount():
            return
        a, b = self._row_values(r), self._row_values(t)
        for row, vals in ((r, b), (t, a)):
            self.table.item(row, COL_NAME).setText(vals[0])
            ident = self.table.item(row, COL_LINE)
            ident.setText(f"{vals[1][0]} / {vals[1][1]}")
            ident.setData(Qt.UserRole, vals[1])
            self.table.cellWidget(row, COL_PTT).deleteLater()
            self.table.cellWidget(row, COL_LATCH).deleteLater()
            self.table.setCellWidget(row, COL_PTT, KeyEdit(vals[2]))
            self.table.setCellWidget(row, COL_LATCH, KeyEdit(vals[3]))
        self.table.setCurrentCell(t, 0)

    def _row_values(self, r: int):
        return (self.table.item(r, COL_NAME).text(), self.table.item(r, COL_LINE).data(Qt.UserRole),
                self.table.cellWidget(r, COL_PTT).spec(), self.table.cellWidget(r, COL_LATCH).spec())

    # -- login and line list

    def _password(self) -> str | None:
        if self.pw.text():
            return self.pw.text()
        return credentials.get_password(self.url.text().strip().rstrip("/"), self.user.text().strip())

    def _login(self) -> None:
        url, user, pw = self.url.text().strip().rstrip("/"), self.user.text().strip(), self._password()
        if not url or not user or not pw:
            self._show_login("Server, Benutzer und Passwort werden zum Anmelden gebraucht.", False)
            return
        self.btn_login.setEnabled(False)
        self.login_result.setText("Melde an …")

        def work() -> None:
            try:
                found = asyncio.run(fetch_available_lines(url, user, pw))
            except (LoginError, LinesError) as e:
                self.loginResult.emit(str(e), False)
                return
            except Exception as e:
                self.loginResult.emit(f"{type(e).__name__}: {e}", False)
                return
            self.linesLoaded.emit(found)
            n = len(found)
            self.loginResult.emit(f"Angemeldet — {n} Leitung{'en' if n != 1 else ''} verfügbar." if n
                                  else "Angemeldet, aber diesem Benutzer ist keine Leitung zugewiesen.", True)

        threading.Thread(target=work, daemon=True).start()

    def _show_login(self, msg: str, ok: bool) -> None:
        self.btn_login.setEnabled(True)
        self.login_result.setText(msg)
        self.login_result.setStyleSheet("color:#2e9d57;" if ok else "color:#d0472f;")

    def _show_lines(self, lines: list[AvailableLine]) -> None:
        self.available = lines
        self.line_pick.clear()
        if not lines:
            self.line_pick.addItem("Keine Leitung verfügbar", None)
            return
        for ln in lines:
            self.line_pick.addItem(ln.label, (ln.production_id, ln.line_id, ln.line_name))

    # -- result

    def _build(self) -> dict:
        data = copy.deepcopy(self._raw)
        url = self.url.text().strip().rstrip("/")
        user = self.user.text().strip()
        if not url or not user:
            raise ValueError("Server und Benutzer fehlen.")
        server = {"url": url, "username": user}
        audio = dict(self._raw.get("audio") or {})
        audio.setdefault("backend", "portaudio")
        for key, combo in (("input_device", self.mic), ("output_device", self.speaker)):
            if combo.value():
                audio[key] = combo.value()
            else:
                audio.pop(key, None)
        audio["mic_gain_db"] = round(self.mic_gain.value(), 1)
        echo, noise, agc = self._current_processing()
        for key, value, default in (("echo_cancellation", echo, False), ("noise_suppression", noise, 0),
                                    ("auto_gain", agc, False)):
            if value == default:
                audio.pop(key, None)  # keep the file free of defaults
            else:
                audio[key] = value
        for key, value, default in (("agc_target_dbfs", self.agc_target.value(), -20),
                                    ("agc_max_gain_db", self.agc_max.value(), 25)):
            if value == default:
                audio.pop(key, None)
            else:
                audio[key] = value
        dim = {}
        for key, value, default in (("amount_db", self.dim_amount.value(), ClientDim().amount_db),
                                    ("when_addressed", self.dim_addressed.isChecked(), True),
                                    ("while_speaking", self.dim_speaking.isChecked(), True),
                                    ("delay_ms", int(self.dim_delay.currentData()), ClientDim().delay_ms)):
            if value != default:
                dim[key] = value
        lines = []
        for r in range(self.table.rowCount()):
            name, (prod, line), ptt, latch = self._row_values(r)
            name = name.strip()
            if not name:
                raise ValueError(f"Die Leitung in Zeile {r + 1} braucht einen Namen.")
            entry = {"name": name, "production_id": prod, "line_id": line,
                     "volume": self._volumes.get(name, 100)}
            if ptt:
                entry["ptt_key"] = ptt
            if latch:
                entry["latch_key"] = latch
            lines.append(entry)
        if not lines:
            raise ValueError("Mindestens eine Leitung hinzufügen.")
        hotkeys = {"global_mute": self.mute_all_key.spec()}
        data.update(server=server, audio=audio, hotkeys={k: v for k, v in hotkeys.items() if v}, line=lines)
        if not data["hotkeys"]:
            data.pop("hotkeys")
        if dim:
            data["dim"] = dim
        else:
            data.pop("dim", None)
        url_text = self.companion.text().strip()
        if url_text and "://" not in url_text:
            url_text = "ws://" + url_text  # "127.0.0.1:12345" is enough
        if url_text:
            data["companion"] = {"url": url_text}
        else:
            data.pop("companion", None)
        # the real check: the same parser the client uses (also catches one key used twice)
        password = self.pw.text() or self._password() or "unchecked"
        try:
            parse_client_config(data, env={}, lookup_password=lambda u, n: password)
        except ConfigError as e:
            raise ValueError(str(e)) from None
        return data

    def _accept(self) -> None:
        try:
            self.result_data = self._build()
        except ValueError as e:
            self.error.setText(str(e))
            return
        self.new_password = self.pw.text() or None
        self.accept()
