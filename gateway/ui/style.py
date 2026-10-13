from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QWidget


def muted(w: QWidget) -> None:
    """Secondary text: the normal text colour pulled towards the background (works in light and dark mode).
    Used instead of setEnabled(False), which renders too faint to read."""
    pal = w.palette()
    fg, bg = pal.color(QPalette.WindowText), pal.color(QPalette.Window)
    mix = QColor(*(round(f * 0.62 + b * 0.38) for f, b in zip((fg.red(), fg.green(), fg.blue()), (bg.red(), bg.green(), bg.blue()))))
    pal.setColor(QPalette.WindowText, mix)
    w.setPalette(pal)
