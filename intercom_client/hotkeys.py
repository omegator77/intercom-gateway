"""Global hotkeys: key combinations -> actions, with key-down and key-up (push-to-talk needs both).

The matching is plain logic (`HotkeyMatcher`) and works on key names, so it is tested without a keyboard.
`HotkeyListener` feeds it from a system-wide keyboard hook (pynput), which also sees keys while another
program has the focus."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("client.hotkeys")

MODIFIERS = ("ctrl", "alt", "shift", "win")

_ALIASES = {
    "control": "ctrl", "ctrl_l": "ctrl", "ctrl_r": "ctrl",
    "shift_l": "shift", "shift_r": "shift",
    "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt", "option": "alt",
    "cmd": "win", "cmd_l": "win", "cmd_r": "win", "super": "win", "meta": "win", "windows": "win",
    "return": "enter", "escape": "esc", "spacebar": "space", "del": "delete", "ins": "insert",
    "pgup": "page_up", "pageup": "page_up", "pgdown": "page_down", "pagedown": "page_down",
    "capslock": "caps_lock", "numlock": "num_lock", "scrolllock": "scroll_lock",
}


class HotkeyError(Exception):
    pass


def normalize_key(name: str) -> str:
    key = name.strip().lower().replace(" ", "_")
    return _ALIASES.get(key, key)


@dataclass(frozen=True)
class Combo:
    mods: frozenset[str]
    key: str

    def __str__(self) -> str:
        return "+".join([m for m in MODIFIERS if m in self.mods] + [self.key])


def parse_combo(text: str) -> Combo:
    """'Ctrl+Shift+F1' -> Combo({'ctrl','shift'}, 'f1'). Exactly one non-modifier key is required."""
    parts = [normalize_key(p) for p in str(text).split("+") if p.strip()]
    if not parts:
        raise ValueError("empty key combination")
    mods = frozenset(p for p in parts if p in MODIFIERS)
    keys = [p for p in parts if p not in MODIFIERS]
    if len(keys) != 1:
        raise ValueError(f"{text!r}: need exactly one key besides the modifiers (Ctrl, Alt, Shift, Win)")
    return Combo(mods, keys[0])


Event = tuple[str, str]  # (action id, "down" | "up")


class HotkeyMatcher:
    """Turns key presses and releases into (action, down/up). A combo fires once per physical press (key
    auto-repeat is ignored) and ends when its key or one of its modifiers is released."""

    def __init__(self, bindings: dict[str, Combo]):
        self._by_combo = {combo: action for action, combo in bindings.items()}
        if len(self._by_combo) != len(bindings):
            raise HotkeyError("two actions use the same key combination")
        self._mods: set[str] = set()
        self._down: set[str] = set()
        self._active: dict[str, Combo] = {}  # action -> combo currently held

    def press(self, key: str) -> list[Event]:
        key = normalize_key(key)
        if key in MODIFIERS:
            self._mods.add(key)
            return []
        if key in self._down:
            return []  # auto-repeat of a key that is already down
        self._down.add(key)
        combo = Combo(frozenset(self._mods), key)
        action = self._by_combo.get(combo)
        if action is None:
            return []
        self._active[action] = combo
        return [(action, "down")]

    def release(self, key: str) -> list[Event]:
        key = normalize_key(key)
        if key in MODIFIERS:
            self._mods.discard(key)
            ended = [a for a, c in self._active.items() if key in c.mods]
        else:
            self._down.discard(key)
            ended = [a for a, c in self._active.items() if c.key == key]
        for action in ended:
            del self._active[action]
        return [(action, "up") for action in ended]

    def release_all(self) -> list[Event]:
        """Everything is let go at once (the hook was restarted or the session was locked)."""
        ended = [(action, "up") for action in self._active]
        self._active.clear()
        self._down.clear()
        self._mods.clear()
        return ended


def pynput_key_name(key) -> str | None:
    """The name pynput gives a key -> our key name. Letters and digits come from the virtual-key code, because
    with Ctrl held pynput reports a control character instead of the letter."""
    vk = getattr(key, "vk", None)
    if isinstance(vk, int):
        if 0x41 <= vk <= 0x5A:
            return chr(vk).lower()
        if 0x30 <= vk <= 0x39:
            return chr(vk)
        if 0x60 <= vk <= 0x69:
            return f"num{vk - 0x60}"
    name = getattr(key, "name", None)  # pynput.keyboard.Key members (f1, space, ctrl_l, ...)
    if name:
        return normalize_key(name)
    char = getattr(key, "char", None)
    return normalize_key(char) if char else None


class HotkeyListener:
    """System-wide keyboard hook. `on_event(action, "down"|"up")` is called from the hook's own thread."""

    def __init__(self, bindings: dict[str, Combo], on_event: Callable[[str, str], None]):
        self.matcher = HotkeyMatcher(bindings)
        self._on_event = on_event
        self._listener = None

    def _dispatch(self, events: list[Event]) -> None:
        for action, edge in events:
            try:
                self._on_event(action, edge)
            except Exception:
                log.exception("hotkey action %s failed", action)

    def start(self) -> None:
        try:
            from pynput import keyboard
        except Exception as e:  # not installed, or no usable backend on this platform
            raise HotkeyError(f"global hotkeys are not available ({type(e).__name__}: {e})") from e

        def on_press(key):
            name = pynput_key_name(key)
            if name:
                self._dispatch(self.matcher.press(name))

        def on_release(key):
            name = pynput_key_name(key)
            if name:
                self._dispatch(self.matcher.release(name))

        self._listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self._listener.daemon = True
        self._listener.start()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        self._dispatch(self.matcher.release_all())  # nothing may stay "held" after the hook is gone
