"""intercom-client [--config FILE] [--state-dir DIR]"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from .. import __version__
from ..paths import default_config, default_log, state_dir
from .window import MainWindow


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="intercom-client", description=__doc__)
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("-c", "--config", default=None, help="config file (default: client.toml in the state dir)")
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--no-hotkeys", action="store_true", help="do not install the global keyboard hook")
    a, qt_args = ap.parse_known_args(argv)
    home = state_dir(a.state_dir)
    home.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(default_log(home), encoding="utf-8"), logging.StreamHandler()])
    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName("Intercom-Client")
    config = Path(a.config) if a.config else default_config(home)
    win = MainWindow(home, config, enable_hotkeys=not a.no_hotkeys)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
