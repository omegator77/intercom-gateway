"""Renders the UI offscreen against a fake gateway control server and saves PNGs, so the layout can be
checked without a display, a sound card or a server.

    python dev/ui_demo.py OUT_DIR
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from gateway import tomlio
from gateway.control import ControlServer
from gateway.ui.settings import SettingsDialog
from gateway.ui.window import MainWindow

out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
out.mkdir(parents=True, exist_ok=True)
home = out / "home"
home.mkdir(exist_ok=True)
cfg = home / "gateway.toml"
tomlio.save(cfg, {
    "server": {"url": "https://intercom.example.com"},
    "audio": {"backend": "portaudio", "playout_ms": 60},
    "line": [
        {"name": "Regie", "production_id": "4", "line_id": "1", "username": "gw-regie", "input_device": "DVS Receive 1-2",
         "output_device": "DVS Transmit 1-2", "input_channels": [0], "output_channels": [0, 1]},
        {"name": "Technik", "production_id": "4", "line_id": "2", "username": "gw-technik", "input_device": "DVS Receive 3-4",
         "output_device": "DVS Transmit 3-4"},
        {"name": "Licht", "production_id": "4", "line_id": "3", "username": "gw-licht", "enabled": False,
         "input_device": "DVS Receive 5-6", "output_device": "DVS Transmit 5-6"},
    ],
})

app = QApplication([])
win = MainWindow(home, cfg)
win.resize(780, 620)
win.show()


def shot(name: str, widget=None) -> None:
    for _ in range(20):
        app.processEvents()
        time.sleep(0.02)
    (widget or win).grab().save(str(out / name))


shot("1_stopped.png")


def line(name, prod, lid, user, i, o, state, ind, outd, **kw):
    return {"line": name, "production_id": prod, "line_id": lid, "username": user, "input_device": i, "output_device": o,
            "state": state, "session": "x", "sessions_started": 1, "uptime_s": kw.get("up", 0), "in_dbfs": ind,
            "out_dbfs": outd, "audio": kw.get("audio", {}), "last_error": kw.get("err")}


status = {"pid": 4242, "version": "0.1.0", "started_at": time.time() - 5025, "server": "https://intercom.example.com",
          "config": str(cfg), "lines": [
    line("Regie", "4", "1", "gw-regie", "DVS Receive 1-2", "DVS Transmit 1-2", "connected", -21.0, -14.5, up=4980,
         audio={"underruns": 0, "in_stalls": 0}),
    line("Technik", "4", "2", "gw-technik", "DVS Receive 3-4", "DVS Transmit 3-4", "backoff", -120.0, -120.0,
         err="Anmeldung fehlgeschlagen: falsches Passwort (HTTP 401)"),
]}

ready = threading.Event()


def serve() -> None:
    async def go() -> None:
        srv = ControlServer(home, lambda: status, lambda: None, str(cfg))
        await srv.start()
        ready.set()
        await asyncio.sleep(8)
        await srv.close()

    asyncio.run(go())


threading.Thread(target=serve, daemon=True).start()
ready.wait(5)
deadline = time.time() + 4
while time.time() < deadline and not win._running:
    app.processEvents()
    time.sleep(0.05)
shot("2_running.png")

win.btn_log.setChecked(True)
(home / "gateway.log").write_text("2026-10-13 12:00:01,000 INFO    gateway: line 'Regie' -> production 4 line 1 as gw-regie\n"
                                   "2026-10-13 12:00:03,500 INFO    gateway.session: [Regie] connected (session 1234, 880 ms)\n", encoding="utf-8")
win._refresh_log()
shot("3_log.png")

dlg = SettingsDialog(cfg, win)
dlg.show()
shot("4_settings.png", dlg)
dlg.list.setCurrentRow(0)
d2 = dlg._dialog(dlg._entries[0].line)
d2.show()
shot("5_line.png", d2)
print("saved to", out)
os._exit(0)
