"""The mouse wheel must scroll the window, not turn the controls under the pointer.

In Qt a combo box, spin box or slider reacts to the wheel as soon as the pointer is over it, even without focus. Scrolling the
settings dialog or the list of lines therefore silently changed sound devices, key choices and line volumes. This filter
passes the wheel on to the surrounding scroll area unless the control has the keyboard focus (clicked into on purpose)."""
from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QEvent, QObject
from PySide6.QtWidgets import QAbstractSlider, QAbstractSpinBox, QApplication, QComboBox, QScrollBar, QWidget

GUARDED = (QComboBox, QAbstractSpinBox, QAbstractSlider)


class WheelGuard(QObject):
    def eventFilter(self, obj, event) -> bool:
        if event.type() != QEvent.Wheel or not isinstance(obj, QWidget):
            return False
        target = obj
        if not isinstance(target, GUARDED) and isinstance(obj.parentWidget(), GUARDED):
            target = obj.parentWidget()  # the line edit inside a spin box or an editable combo box
        if not isinstance(target, GUARDED) or isinstance(target, QScrollBar):
            return False  # (the scroll bars are the thing that is supposed to move)
        focus = QApplication.focusWidget()
        if target.hasFocus() or (focus is not None and target.isAncestorOf(focus)):
            return False  # the user clicked into this control: the wheel is meant for it
        event.ignore()
        parent = target.parentWidget()
        if parent is not None:
            QCoreApplication.sendEvent(parent, event)  # let the scroll area around it scroll
        return True


_guard: WheelGuard | None = None


def install() -> None:
    """Once per program (safe to call from every window)."""
    global _guard
    app = QApplication.instance()
    if app is None or _guard is not None and _guard.parent() is app:
        return
    _guard = WheelGuard(app)
    app.installEventFilter(_guard)
