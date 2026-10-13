"""intercom-gateway-ui [--config FILE] [--state-dir DIR]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from .. import __version__
from ..paths import default_config, state_dir
from .window import MainWindow


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="intercom-gateway-ui", description=__doc__)
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("-c", "--config", default=None, help="config file (default: gateway.toml in the state dir)")
    ap.add_argument("--state-dir", default=None)
    a, qt_args = ap.parse_known_args(argv)
    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName("Intercom-Gateway")
    home = state_dir(a.state_dir)
    home.mkdir(parents=True, exist_ok=True)
    config = Path(a.config) if a.config else default_config(home)
    win = MainWindow(home, config)
    win.show()
    if not config.exists():
        QTimer.singleShot(0, win.open_settings)  # first start: nothing to show yet, go straight to setup
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
