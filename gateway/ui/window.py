"""Main window: start/stop the gateway, one card per line with state and level meters, optional log."""
from __future__ import annotations

import html
import threading
import time
from pathlib import Path

from PySide6.QtCore import QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPalette
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QSizePolicy, QToolButton, QVBoxLayout, QWidget, QMainWindow)

from .. import launcher, tomlio
from ..control import ControlError, fetch_status, read_control
from ..paths import default_config, default_log
from .settings import SettingsDialog
from .style import muted

STATE_TEXT = {
    "idle": ("Bereit", "#7a7f87"),
    "login": ("Anmeldung …", "#c98a00"),
    "connecting": ("Verbindet …", "#c98a00"),
    "connected": ("Verbunden", "#2e9d57"),
    "backoff": ("Neuer Versuch …", "#d0472f"),
    "stopped": ("Gestoppt", "#7a7f87"),
    "off": ("Aus", "#7a7f87"),
    "disabled": ("Deaktiviert", "#7a7f87"),
}
FLOOR_DB = -60.0


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


class Meter(QWidget):
    """Horizontal level bar, -60 .. 0 dBFS."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(10)
        self.setMinimumWidth(110)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._db = -120.0

    def set_db(self, db: float) -> None:
        if abs(db - self._db) >= 0.5:
            self._db = db
            self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        track = QColor(self.palette().color(QPalette.Mid))
        track.setAlpha(90)
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(r, 3, 3)
        frac = max(0.0, min(1.0, (self._db - FLOOR_DB) / -FLOOR_DB))
        if frac > 0:
            grad = QLinearGradient(r.left(), 0, r.right(), 0)
            grad.setColorAt(0.0, QColor("#2e9d57"))
            grad.setColorAt(0.72, QColor("#d9b100"))  # about -17 dBFS
            grad.setColorAt(0.92, QColor("#d0472f"))  # about -5 dBFS
            p.setBrush(grad)
            p.setClipRect(QRectF(r.left(), r.top(), r.width() * frac, r.height()))
            p.drawRoundedRect(r, 3, 3)


def db_text(db: float) -> str:
    return "–∞" if db <= -100 else f"{db:.0f} dB"


def people_html(people: list[dict] | None) -> str:
    """'Who else is on the line' as one line of rich text; '' when the server's list is not known."""
    if people is None:
        return ""
    if not people:
        return "<span style='color:#7a7f87;'>Sonst niemand in der Leitung</span>"
    items = []
    for p in people:
        dot = "#2e9e57" if p["active"] else "#9aa0a6"
        tags = ("" if p["active"] else " (inaktiv)") + (" · WHIP" if p.get("whip") else "")
        items.append(f"<span style='color:{dot};'>●</span> {html.escape(p['name'])}"
                     f"<span style='color:#7a7f87;'>{tags}</span>")
    return "In der Leitung: " + "&nbsp;&nbsp;&nbsp;".join(items)


class LineCard(QFrame):
    def __init__(self, name: str) -> None:
        super().__init__()
        self.setFrameShape(QFrame.StyledPanel)
        self.name = name
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
        head.addWidget(self.title)
        head.addWidget(self.pill)
        head.addStretch(1)
        head.addWidget(self.since)
        outer.addLayout(head)

        self.where = QLabel()
        muted(self.where)
        outer.addWidget(self.where)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(2, 1)
        self.in_dev, self.out_dev = QLabel(), QLabel()
        self.in_meter, self.out_meter = Meter(), Meter()
        self.in_val, self.out_val = QLabel(), QLabel()
        for row, (cap, dev, meter, val) in enumerate((("Eingang", self.in_dev, self.in_meter, self.in_val),
                                                       ("Ausgang", self.out_dev, self.out_meter, self.out_val))):
            c = QLabel(cap)
            c.setMinimumWidth(56)
            val.setMinimumWidth(48)
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            dev.setMinimumWidth(190)
            dev.setTextInteractionFlags(Qt.TextSelectableByMouse)
            grid.addWidget(c, row, 0)
            grid.addWidget(dev, row, 1)
            grid.addWidget(meter, row, 2)
            grid.addWidget(val, row, 3)
        outer.addLayout(grid)

        self.people = QLabel()
        self.people.setWordWrap(True)
        self.people.setTextFormat(Qt.RichText)
        outer.addWidget(self.people)

        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setTextInteractionFlags(Qt.TextSelectableByMouse)
        outer.addWidget(self.note)

    def _set_state(self, key: str) -> None:
        text, color = STATE_TEXT.get(key, (key, "#7a7f87"))
        self.pill.setText(text)
        self.pill.setStyleSheet(f"background:{color}; color:white; border-radius:9px; padding:2px 10px; font-weight:600;")

    def show_config(self, line: dict) -> None:
        """Gateway not running: show what is configured."""
        self._set_state("off" if line.get("enabled", True) else "disabled")
        self._fill_static(line.get("production_id"), line.get("line_id"), line.get("username"),
                          line.get("input_device"), line.get("output_device"))
        self.since.setText("")
        for m, v in ((self.in_meter, self.in_val), (self.out_meter, self.out_val)):
            m.set_db(-120)
            v.setText("")
        self.note.setVisible(False)
        self.people.setVisible(False)

    def show_status(self, s: dict) -> None:
        self._set_state(s["state"])
        self._fill_static(s.get("production_id"), s.get("line_id"), s.get("username"),
                          s.get("input_device"), s.get("output_device"))
        up = s.get("uptime_s", 0)
        self.since.setText(f"verbunden seit {fmt_duration(up)}" if s["state"] == "connected" else "")
        self.in_meter.set_db(s["in_dbfs"])
        self.out_meter.set_db(s["out_dbfs"])
        self.in_val.setText(db_text(s["in_dbfs"]) if s.get("input_device") else "")
        self.out_val.setText(db_text(s["out_dbfs"]) if s.get("output_device") else "")

        text = people_html(s.get("participants")) if s["state"] == "connected" else ""
        self.people.setText(text)
        self.people.setVisible(bool(text))

        parts, bad = [], False
        a = s.get("audio") or {}
        if s.get("last_error"):
            parts.append(s["last_error"])
            bad = True
        if s["state"] == "connected" and a:
            if a.get("in_stalls"):
                parts.append(f"Eingangsgerät lieferte {a['in_stalls']}× keine Daten")
            if a.get("underruns"):
                parts.append(f"{a['underruns']} Aussetzer im Ausgang")
        self.note.setText("  ·  ".join(parts))
        self.note.setVisible(bool(parts))
        self.note.setStyleSheet("color:#d0472f;" if bad else "")

    def _fill_static(self, prod, line, user, in_dev, out_dev) -> None:
        self.where.setText(f"Produktion {prod} · Leitung {line} · Benutzer {user}")
        self.in_dev.setText(in_dev or "—")
        self.out_dev.setText(out_dev or "—")


class Poller(QThread):
    """Asks the gateway for its status a few times per second; emits None when it is not running."""
    status = Signal(object)

    def __init__(self, home: Path) -> None:
        super().__init__()
        self._home = home
        self._quit = threading.Event()

    def run(self) -> None:
        while not self._quit.is_set():
            data = None
            info = read_control(self._home)
            if info:
                try:
                    data = fetch_status(info, timeout=1.5)
                except ControlError:
                    data = None
            self.status.emit(data)
            self._quit.wait(0.3)

    def shutdown(self) -> None:
        self._quit.set()
        self.wait(3000)


class MainWindow(QMainWindow):
    actionDone = Signal(str)

    def __init__(self, home: Path, config_path: Path | None = None) -> None:
        super().__init__()
        self.home = home
        self.config_path = config_path or default_config(home)
        self.setWindowTitle("Intercom-Gateway")
        self.resize(780, 600)
        self._busy = False
        self._running = False
        self._cards: dict[str, LineCard] = {}
        self._config_mtime: float | None = -2.0  # sentinel: never loaded
        self._server_url = ""
        self._config_lines: list[dict] = []
        self._config_error = ""
        self._action_error = ""  # message of the last start/stop attempt, shown until the next one
        self._last_status: dict | None = None
        self._log_state = (0, 0.0)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 14, 16, 12)
        root.setSpacing(10)

        top = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(2)
        self.headline = QLabel()
        f = self.headline.font()
        f.setPointSizeF(f.pointSizeF() * 1.4)
        f.setBold(True)
        self.headline.setFont(f)
        self.subline = QLabel()
        muted(self.subline)
        col.addWidget(self.headline)
        col.addWidget(self.subline)
        top.addLayout(col, 1)
        self.btn_settings = QPushButton("Einstellungen …")
        self.btn_start = QPushButton("Starten")
        self.btn_stop = QPushButton("Stoppen")
        self.btn_start.setDefault(True)
        for b in (self.btn_settings, self.btn_stop, self.btn_start):
            b.setMinimumHeight(32)
            top.addWidget(b)
        root.addLayout(top)

        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setVisible(False)
        self.banner.setStyleSheet("background:#fbe9e5; color:#7a2515; border-radius:4px; padding:8px 10px;")
        root.addWidget(self.banner)

        self.list_host = QWidget()
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(10)
        self.list_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(self.list_host)
        root.addWidget(scroll, 1)

        self.empty = QLabel("Noch keine Leitung eingerichtet.\nÜber „Einstellungen …“ eine Leitung hinzufügen.")
        self.empty.setAlignment(Qt.AlignCenter)
        muted(self.empty)
        root.addWidget(self.empty)

        foot = QHBoxLayout()
        self.btn_log = QToolButton()
        self.btn_log.setText("Protokoll")
        self.btn_log.setCheckable(True)
        self.btn_log.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.hint = QLabel("Das Gateway läuft unabhängig von diesem Fenster weiter.")
        muted(self.hint)
        foot.addWidget(self.btn_log)
        foot.addStretch(1)
        foot.addWidget(self.hint)
        root.addLayout(foot)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setFixedHeight(170)
        self.log.setVisible(False)
        self.log.setLineWrapMode(QPlainTextEdit.NoWrap)
        mono = self.log.font()
        mono.setFamily("Consolas")
        mono.setStyleHint(mono.StyleHint.Monospace)
        self.log.setFont(mono)
        root.addWidget(self.log)

        self.btn_start.clicked.connect(self.start_gateway)
        self.btn_stop.clicked.connect(self.stop_gateway)
        self.btn_settings.clicked.connect(self.open_settings)
        self.btn_log.toggled.connect(self._toggle_log)
        self.actionDone.connect(self._action_done)

        self.poller = Poller(home)
        self.poller.status.connect(self._on_status)
        self._log_timer = QTimer(self)
        self._log_timer.timeout.connect(self._refresh_log)
        self._load_config()
        self._render()
        self.poller.start()

    # ------------------------------------------------------------------ config / rendering

    def _load_config(self) -> None:
        try:
            mtime = self.config_path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime == self._config_mtime:
            return
        self._config_mtime = mtime
        try:
            raw = tomlio.load_raw(self.config_path)
            self._config_lines = list(raw.get("line", []))
            self._server_url = (raw.get("server") or {}).get("url", "")
            self._config_error = ""
        except Exception as e:
            self._config_lines, self._server_url = [], ""
            self._config_error = f"Konfiguration nicht lesbar: {e}"

    def _on_status(self, data: dict | None) -> None:
        self._load_config()
        self._running = data is not None
        self._last_status = data
        self._render()

    def _render(self) -> None:
        data = self._last_status
        if data:
            self.headline.setText("Gateway läuft")
            since = fmt_duration(time.time() - data["started_at"])
            self.subline.setText(f"{data.get('server', '')} · seit {since} · PID {data['pid']}")
            items = [(l["line"], l) for l in data["lines"]]
        else:
            self.headline.setText("Gateway gestoppt")
            self.subline.setText(self._server_url if self._config_lines else "")
            items = [(l.get("name", "?"), l) for l in self._config_lines]
        names = [n for n, _ in items]
        if names != list(self._cards):
            self._rebuild_cards(names)
        for name, item in items:
            card = self._cards[name]
            card.show_status(item) if data else card.show_config(item)
        self.empty.setVisible(not items)
        err = self._action_error or self._config_error
        self.banner.setText(err)
        self.banner.setVisible(bool(err))
        self.btn_start.setEnabled(not self._busy and not self._running and bool(self._config_lines))
        self.btn_stop.setEnabled(not self._busy and self._running)
        self.btn_settings.setEnabled(not self._busy)

    def _rebuild_cards(self, names: list[str]) -> None:
        for card in self._cards.values():
            card.setParent(None)
            card.deleteLater()
        self._cards = {}
        for n in names:
            card = LineCard(n)
            self._cards[n] = card
            self.list_layout.insertWidget(self.list_layout.count() - 1, card)

    # ------------------------------------------------------------------ actions

    def _run_action(self, headline: str, fn) -> None:
        self._busy = True
        self._action_error = ""
        self.headline.setText(headline)
        self._render()

        def work() -> None:
            try:
                fn()
                msg = ""
            except Exception as e:
                msg = str(e) or type(e).__name__
            self.actionDone.emit(msg)

        threading.Thread(target=work, daemon=True).start()

    def _action_done(self, error: str) -> None:
        self._busy = False
        self._action_error = error
        self._render()

    def start_gateway(self) -> None:
        self._run_action("Gateway wird gestartet …", lambda: launcher.start(self.config_path, self.home))

    def stop_gateway(self) -> None:
        lines = len(self._last_status["lines"]) if self._last_status else 0
        box = QMessageBox(QMessageBox.Question, "Gateway stoppen",
                          f"Das Gateway stoppen? Alle {lines} Leitungen werden getrennt und die Audioverbindung "
                          "zur Dante-Seite endet.", QMessageBox.Yes | QMessageBox.Cancel, self)
        box.setDefaultButton(QMessageBox.Cancel)
        box.button(QMessageBox.Yes).setText("Stoppen")
        box.button(QMessageBox.Cancel).setText("Abbrechen")
        if box.exec() == QMessageBox.Yes:
            self._run_action("Gateway wird gestoppt …", lambda: launcher.stop(self.home))

    def open_settings(self) -> None:
        dlg = SettingsDialog(self.config_path, self)
        if dlg.exec() != SettingsDialog.Accepted:
            return
        self._config_mtime = -1.0  # force reload
        self._load_config()
        self._render()
        if self._running:
            box = QMessageBox(QMessageBox.Question, "Neu starten?",
                              "Die Einstellungen sind gespeichert und gelten ab dem nächsten Start des Gateways.\n"
                              "Jetzt neu starten? Dabei werden alle Leitungen kurz getrennt.",
                              QMessageBox.Yes | QMessageBox.No, self)
            box.setDefaultButton(QMessageBox.No)
            box.button(QMessageBox.Yes).setText("Neu starten")
            box.button(QMessageBox.No).setText("Später")
            if box.exec() == QMessageBox.Yes:
                self._run_action("Gateway wird neu gestartet …", self._restart)

    def _restart(self) -> None:
        launcher.stop(self.home)
        launcher.start(self.config_path, self.home)

    # ------------------------------------------------------------------ log

    def _toggle_log(self, on: bool) -> None:
        self.log.setVisible(on)
        if on:
            self._log_state = (0, 0.0)
            self._refresh_log()
            self._log_timer.start(1000)
        else:
            self._log_timer.stop()

    def _refresh_log(self) -> None:
        path = default_log(self.home)
        try:
            st = path.stat()
        except OSError:
            self.log.setPlainText("(noch kein Protokoll)")
            return
        if (st.st_size, st.st_mtime) == self._log_state:
            return
        self._log_state = (st.st_size, st.st_mtime)
        with path.open("rb") as f:
            f.seek(max(0, st.st_size - 60_000))
            text = f.read().decode("utf-8", "replace")
        lines = text.splitlines()[-300:]
        bar = self.log.verticalScrollBar()
        at_end = bar.value() >= bar.maximum() - 4
        self.log.setPlainText("\n".join(lines))
        if at_end:
            bar.setValue(bar.maximum())

    def closeEvent(self, event) -> None:
        self.poller.shutdown()  # only the window goes away; the gateway process keeps running
        super().closeEvent(event)
