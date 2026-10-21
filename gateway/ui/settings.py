"""Settings: server, audio mode and the list of lines; one dialog per line. Writes the same TOML file the
CLI reads. Passwords go to the Windows Credential Manager, never into the file."""
from __future__ import annotations

import asyncio
import copy
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                               QPushButton, QSpinBox, QVBoxLayout, QWidget)

from .. import credentials, tomlio
from ..auth import LoginError
from ..lines import AvailableLine, LinesError, fetch_available_lines
from . import devices
from .style import muted

LINE_URL = re.compile(r"/production(?:-calls)?/production/(\d+)/line/(\d+)")


@dataclass
class Entry:
    """One line in the settings list plus what the file does not hold: a password typed this session and the
    (server, account) it was stored under before, so a rename can carry the stored password along."""
    line: dict
    new_password: str | None = None
    orig_key: tuple[str, str] = field(default=("", ""))


def parse_channels(text: str) -> list[int]:
    """'1' -> [0], '1, 2' -> [0, 1] (the UI counts from 1, the file from 0)."""
    parts = [p for p in re.split(r"[,\s;]+", text.strip()) if p]
    if not parts or not all(p.isdigit() and int(p) >= 1 for p in parts):
        raise ValueError("Kanäle als Zahlen ab 1 angeben, z. B. „1“ oder „1, 2“.")
    return sorted({int(p) - 1 for p in parts})


def channels_text(chans: list[int] | None, default: str) -> str:
    return ", ".join(str(c + 1) for c in chans) if chans else default


class DeviceCombo(QComboBox):
    NONE = "— keins —"

    def __init__(self, names: list[tuple[str, int]], current: str | None) -> None:
        super().__init__()
        self.setEditable(True)
        self.setMinimumWidth(280)
        self.addItem(self.NONE, "")
        for name, ch in names:
            self.addItem(f"{name}  ({ch} Kanäle)", name)
        if current and self.findData(current) < 0:  # configured device not present right now: keep it visible
            self.addItem(f"{current}  (nicht gefunden)", current)
        self.setCurrentIndex(max(0, self.findData(current or "")))

    def value(self) -> str:
        text = self.currentText().strip()
        idx = self.currentIndex()
        if idx >= 0 and text == self.itemText(idx):
            return self.itemData(idx) or ""
        return "" if text == self.NONE else text  # typed by hand: a name fragment is fine, the gateway matches it


class LineDialog(QDialog):
    loginResult = Signal(str, bool)
    linesLoaded = Signal(object)  # list[AvailableLine]

    def __init__(self, line: dict, server_url: str, backend: str, inputs, outputs, stored_pw: bool,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Leitung")
        self._orig = copy.deepcopy(line)
        self._server_url = server_url
        self._stored_pw = stored_pw
        self.new_password: str | None = None
        self.result_line: dict | None = None

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.name = QLineEdit(line.get("name", ""))
        self.name.setPlaceholderText("z. B. Regie")
        self._auto_name = ""  # the name taken from the list, so a later pick may replace it but never a typed one
        self.link = QLineEdit()
        self.link.setPlaceholderText("Link der Leitung einfügen, z. B. …/production/4/line/1")
        self.prod = QLineEdit(str(line.get("production_id", "")))
        self.line_id = QLineEdit(str(line.get("line_id", "")))
        self.link.textChanged.connect(self._from_link)
        ids = QHBoxLayout()
        ids.addWidget(QLabel("Produktion"))
        ids.addWidget(self.prod)
        ids.addWidget(QLabel("Leitung"))
        ids.addWidget(self.line_id)
        self.line_pick = QComboBox()
        self.line_pick.setMinimumWidth(320)
        self.line_pick.currentIndexChanged.connect(self._picked)
        self._fill_line_pick([])
        self.btn_manual = QPushButton("Manuell eintragen …")
        self.btn_manual.setCheckable(True)
        self.manual = QWidget()
        manual_form = QFormLayout(self.manual)
        manual_form.setContentsMargins(0, 0, 0, 0)
        manual_form.addRow("Link", self.link)
        manual_form.addRow("", ids)
        self.manual.setVisible(False)
        self.btn_manual.toggled.connect(self.manual.setVisible)
        self.user = QLineEdit(line.get("username", ""))
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.Password)
        if line.get("password_env"):
            self.pw.setPlaceholderText(f"aus Umgebungsvariable {line['password_env']} — leer lassen")
        elif line.get("password"):
            self.pw.setPlaceholderText("steht im Klartext in der Datei — hier neu eingeben, um es sicher zu speichern")
        elif stored_pw:
            self.pw.setPlaceholderText("gespeichert — leer lassen, um es zu behalten")
        else:
            self.pw.setPlaceholderText("Passwort dieses Benutzers")
        test_row = QHBoxLayout()
        self.btn_test = QPushButton("Anmelden und Leitungen laden")
        self.test_result = QLabel()
        self.test_result.setWordWrap(True)
        test_row.addWidget(self.btn_test)
        test_row.addWidget(self.test_result, 1)
        self.btn_test.clicked.connect(self._test_login)
        self.loginResult.connect(self._show_login)
        self.linesLoaded.connect(self._show_lines)
        form.addRow("Benutzer", self.user)
        form.addRow("Passwort", self.pw)
        form.addRow("", test_row)
        form.addRow("Leitung", self.line_pick)
        form.addRow("Name", self.name)
        form.addRow("", self.btn_manual)
        form.addRow(self.manual)

        self.synthetic = backend == "synthetic"
        box_in = QGroupBox("Eingang — Audio aus dem Dante-Netz in die Leitung")
        f_in = QFormLayout(box_in)
        self.in_dev = DeviceCombo(inputs, line.get("input_device"))
        self.in_ch = QLineEdit(channels_text(line.get("input_channels"), "1"))
        self.in_ch.setToolTip("Mehrere Kanäle werden zu Mono gemischt")
        self.in_gain = _gain_spin(line.get("in_gain_db", 0.0))
        f_in.addRow("Gerät", self.in_dev)
        f_in.addRow("Kanäle", self.in_ch)
        f_in.addRow("Verstärkung", self.in_gain)

        box_out = QGroupBox("Ausgang — Audio der Leitung ins Dante-Netz")
        f_out = QFormLayout(box_out)
        self.out_dev = DeviceCombo(outputs, line.get("output_device"))
        self.out_ch = QLineEdit(channels_text(line.get("output_channels"), "1, 2"))
        self.out_ch.setToolTip("Die Leitung ist Mono und wird auf alle genannten Kanäle gelegt")
        self.out_gain = _gain_spin(line.get("out_gain_db", 0.0))
        f_out.addRow("Gerät", self.out_dev)
        f_out.addRow("Kanäle", self.out_ch)
        f_out.addRow("Verstärkung", self.out_gain)
        self._boxes = (box_in, box_out)  # not in the layout in test mode: without this reference Qt deletes them

        self.tone = None
        extra = QVBoxLayout()
        if self.synthetic:
            self.tone = QDoubleSpinBox()
            self.tone.setRange(0, 8000)
            self.tone.setSuffix(" Hz")
            self.tone.setSpecialValueText("kein Testton")
            self.tone.setValue(float(line.get("tone_hz") or 0))
            f_t = QFormLayout()
            f_t.addRow("Testton (nur Testmodus)", self.tone)
            extra.addLayout(f_t)

        self.error = QLabel()
        self.error.setStyleSheet("color:#d0472f;")
        self.error.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Übernehmen")
        buttons.button(QDialogButtonBox.Cancel).setText("Abbrechen")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.addLayout(form)
        if not self.synthetic:
            lay.addWidget(box_in)
            lay.addWidget(box_out)
        else:
            hint = QLabel("Testmodus: keine Soundkarte, die Leitung sendet einen Testton.")
            muted(hint)
            lay.addWidget(hint)
            lay.addLayout(extra)
        lay.addWidget(self.error)
        lay.addWidget(buttons)
        self.setMinimumWidth(560)

    def _from_link(self, text: str) -> None:
        m = LINE_URL.search(text)
        if m:
            self.prod.setText(m.group(1))
            self.line_id.setText(m.group(2))

    # -- login test

    def _typed_or_known_password(self) -> str | None:
        if self.pw.text():
            return self.pw.text()
        if self._orig.get("password"):
            return self._orig["password"]
        if self._orig.get("password_env"):
            import os

            return os.environ.get(self._orig["password_env"])
        return credentials.get_password(self._server_url, self._orig.get("username", "")) or \
            credentials.get_password(self._server_url, self.user.text().strip())

    def _test_login(self) -> None:
        user, pw, url = self.user.text().strip(), self._typed_or_known_password(), self._server_url.strip()
        if not url or not user or not pw:
            self._show_login("Server, Benutzer und Passwort werden zum Testen gebraucht.", False)
            return
        self.btn_test.setEnabled(False)
        self.test_result.setText("Melde an …")

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
            self.loginResult.emit(
                f"Angemeldet — {n} Leitung{'en' if n != 1 else ''} verfügbar." if n
                else "Angemeldet, aber diesem Benutzer ist keine Leitung zugewiesen.", True)

        threading.Thread(target=work, daemon=True).start()

    # -- line list

    def _fill_line_pick(self, lines: list[AvailableLine]) -> None:
        """Items carry (production_id, line_id, line_name). Without a list the only item is the configured line
        (or a hint); with one, the configured line stays selected, even if the account no longer offers it."""
        cur = (self.prod.text().strip(), self.line_id.text().strip())
        pick = self.line_pick
        pick.blockSignals(True)
        pick.clear()
        if not lines:
            if all(cur):
                pick.addItem(f"Produktion {cur[0]} / Leitung {cur[1]}  (aktuell)", (*cur, ""))
            else:
                pick.addItem("Erst anmelden, dann Leitung wählen …", None)
        else:
            pick.addItem("— Leitung wählen —", None)
            for ln in lines:
                pick.addItem(ln.label, (ln.production_id, ln.line_id, ln.line_name))
            if all(cur):
                for i in range(1, pick.count()):
                    if tuple(pick.itemData(i)[:2]) == cur:
                        pick.setCurrentIndex(i)
                        break
                else:
                    pick.insertItem(1, f"Produktion {cur[0]} / Leitung {cur[1]}  (nicht in der Liste)", (*cur, ""))
                    pick.setCurrentIndex(1)
        pick.blockSignals(False)

    def _show_lines(self, lines: list[AvailableLine]) -> None:
        self._fill_line_pick(lines)

    def _picked(self, index: int) -> None:
        data = self.line_pick.itemData(index)
        if not data:
            return
        prod, lid, name = data
        self.prod.setText(prod)
        self.line_id.setText(lid)
        if name and (not self.name.text().strip() or self.name.text() == self._auto_name):
            self.name.setText(name)
            self._auto_name = name

    def _show_login(self, msg: str, ok: bool) -> None:
        self.btn_test.setEnabled(True)
        self.test_result.setText(msg)
        self.test_result.setStyleSheet("color:#2e9d57;" if ok else "color:#d0472f;")

    # -- result

    def _accept(self) -> None:
        try:
            self.result_line = self._build()
        except ValueError as e:
            self.error.setText(str(e))
            return
        self.new_password = self.pw.text() or None
        self.accept()

    def _build(self) -> dict:
        name = self.name.text().strip()
        if not name:
            raise ValueError("Die Leitung braucht einen Namen.")
        prod, lid, user = self.prod.text().strip(), self.line_id.text().strip(), self.user.text().strip()
        if not prod or not lid:
            raise ValueError("Keine Leitung gewählt — erst anmelden und eine Leitung aus der Liste wählen "
                             "(oder unter „Manuell eintragen“ angeben).")
        if not user:
            raise ValueError("Der Benutzername fehlt.")
        line = copy.deepcopy(self._orig)
        line.update(name=name, production_id=prod, line_id=lid, username=user)
        if not self.synthetic:
            ind, outd = self.in_dev.value(), self.out_dev.value()
            if not ind and not outd:
                raise ValueError("Mindestens ein Gerät (Eingang oder Ausgang) auswählen.")
            for key, dev in (("input_device", ind), ("output_device", outd)):
                if dev:
                    line[key] = dev
                else:
                    line.pop(key, None)
            line["input_channels"] = parse_channels(self.in_ch.text() or "1")
            line["output_channels"] = parse_channels(self.out_ch.text() or "1, 2")
        line["in_gain_db"] = round(self.in_gain.value(), 1)
        line["out_gain_db"] = round(self.out_gain.value(), 1)
        if self.tone is not None:
            if self.tone.value() > 0:
                line["tone_hz"] = self.tone.value()
            else:
                line.pop("tone_hz", None)
        if self.pw.text():  # a newly typed password replaces any plaintext/env source
            line.pop("password", None)
            line.pop("password_env", None)
        return line


def _gain_spin(value) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(-40, 40)
    s.setDecimals(1)
    s.setSingleStep(0.5)
    s.setSuffix(" dB")
    s.setValue(float(value or 0.0))
    return s


class SettingsDialog(QDialog):
    def __init__(self, config_path: Path, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Einstellungen")
        self.config_path = config_path
        self.raw = tomlio.load_raw(config_path)
        self.raw.setdefault("server", {"url": ""})
        self.raw.setdefault("audio", {"backend": "portaudio", "playout_ms": 60})
        url0 = self.raw["server"].get("url", "")
        self._entries = [Entry(copy.deepcopy(l), None, (url0, l.get("username", ""))) for l in self.raw.get("line", [])]
        self._inputs, self._outputs = devices.list_devices()

        form = QFormLayout()
        self.url = QLineEdit(self.raw["server"].get("url", ""))
        self.url.setPlaceholderText("https://intercom.example.com")
        self.mode = QComboBox()
        self.mode.addItem("Soundkarte (Dante Virtual Soundcard o. ä.)", "portaudio")
        self.mode.addItem("Testmodus ohne Soundkarte", "synthetic")
        self.mode.setCurrentIndex(max(0, self.mode.findData(self.raw["audio"].get("backend", "portaudio"))))
        self.playout = QSpinBox()
        self.playout.setRange(20, 500)
        self.playout.setSuffix(" ms")
        self.playout.setValue(int(self.raw["audio"].get("playout_ms", 60)))
        self.playout.setToolTip("Puffer vor der Wiedergabe: kleiner = weniger Verzögerung, größer = robuster")
        form.addRow("Server", self.url)
        form.addRow("Audio", self.mode)
        form.addRow("Puffer", self.playout)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _: self._edit())
        self.btn_add = QPushButton("Hinzufügen …")
        self.btn_edit = QPushButton("Bearbeiten …")
        self.btn_del = QPushButton("Entfernen")
        self.btn_refresh = QPushButton("Geräte neu einlesen")
        self.btn_add.clicked.connect(self._add)
        self.btn_edit.clicked.connect(self._edit)
        self.btn_del.clicked.connect(self._remove)
        self.btn_refresh.clicked.connect(self._refresh_devices)
        side = QVBoxLayout()
        for b in (self.btn_add, self.btn_edit, self.btn_del):
            side.addWidget(b)
        side.addStretch(1)
        side.addWidget(self.btn_refresh)
        row = QHBoxLayout()
        row.addWidget(self.list, 1)
        row.addLayout(side)

        self.error = QLabel()
        self.error.setStyleSheet("color:#d0472f;")
        self.error.setWordWrap(True)
        self.note = QLabel("Das Häkchen schaltet eine Leitung ein oder aus. Änderungen gelten ab dem nächsten Start.")
        muted(self.note)
        self.note.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("Speichern")
        buttons.button(QDialogButtonBox.Cancel).setText("Abbrechen")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.addLayout(form)
        lay.addWidget(QLabel("Leitungen"))
        lay.addLayout(row, 1)
        lay.addWidget(self.note)
        lay.addWidget(self.error)
        lay.addWidget(buttons)
        self.resize(620, 480)
        self._fill_list()

    def _fill_list(self, select: int = 0) -> None:
        self.list.clear()
        for e in self._entries:
            l = e.line
            it = QListWidgetItem(self._label(l))
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if l.get("enabled", True) else Qt.Unchecked)
            self.list.addItem(it)
        if self._entries:
            self.list.setCurrentRow(min(select, len(self._entries) - 1))

    @staticmethod
    def _label(l: dict) -> str:
        dev = " · ".join(x for x in (l.get("input_device"), l.get("output_device")) if x)
        return f"{l.get('name', '?')}   —   Produktion {l.get('production_id')} / Leitung {l.get('line_id')}" + \
               (f"   —   {dev}" if dev else "")

    def _refresh_devices(self) -> None:
        devices.refresh()
        self._inputs, self._outputs = devices.list_devices()
        self.error.setText(f"{len(self._inputs)} Eingangs- und {len(self._outputs)} Ausgangsgeräte gefunden.")

    def _dialog(self, line: dict) -> LineDialog:
        url = self.url.text().strip().rstrip("/")
        stored = bool(credentials.get_password(url, line.get("username", ""))) if line.get("username") else False
        return LineDialog(line, url, self.mode.currentData(), self._inputs, self._outputs, stored, self)

    def _add(self) -> None:
        dlg = self._dialog({})
        if dlg.exec() == QDialog.Accepted:
            self._entries.append(Entry(dlg.result_line, dlg.new_password))
            self._fill_list(len(self._entries) - 1)

    def _edit(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        entry = self._entries[row]
        dlg = self._dialog(entry.line)
        if dlg.exec() == QDialog.Accepted:
            entry.line = dlg.result_line
            entry.new_password = dlg.new_password or entry.new_password
            self._fill_list(row)

    def _remove(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        name = self._entries[row].line.get("name", "")
        if QMessageBox.question(self, "Leitung entfernen", f"Leitung „{name}“ entfernen?") == QMessageBox.Yes:
            del self._entries[row]
            self._fill_list(row)

    def _save(self) -> None:
        url = self.url.text().strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            self.error.setText("Die Server-Adresse muss mit http:// oder https:// beginnen.")
            return
        names = [e.line["name"] for e in self._entries]
        if len(set(names)) != len(names):
            self.error.setText("Zwei Leitungen heißen gleich — Namen müssen eindeutig sein.")
            return
        if not self._entries:
            self.error.setText("Mindestens eine Leitung einrichten.")
            return
        for i, e in enumerate(self._entries):
            if self.list.item(i).checkState() == Qt.Checked:
                e.line.pop("enabled", None)  # the default; keeps the file tidy
            else:
                e.line["enabled"] = False
        try:
            for e in self._entries:
                user = e.line["username"]
                old_url, old_user = e.orig_key
                if e.new_password:
                    credentials.set_password(url, user, e.new_password)
                elif (old_url, old_user) != (url, user) and old_user and not e.line.get("password_env")                         and not e.line.get("password"):
                    # server or account renamed: carry the stored password over to the new key
                    prev = credentials.get_password(old_url, old_user)
                    if prev and not credentials.get_password(url, user):
                        credentials.set_password(url, user, prev)
        except Exception as ex:
            self.error.setText(f"Das Passwort konnte nicht im Anmeldeinformationsspeicher abgelegt werden: {ex}")
            return
        self.raw["server"]["url"] = url
        self.raw["audio"]["backend"] = self.mode.currentData()
        self.raw["audio"]["playout_ms"] = self.playout.value()
        self.raw["line"] = [e.line for e in self._entries]
        try:
            tomlio.save(self.config_path, self.raw)
        except Exception as e:
            self.error.setText(f"Speichern fehlgeschlagen: {e}")
            return
        self.accept()
