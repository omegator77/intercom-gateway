"""Talk state of one line: hold-to-talk (push-to-talk) and latch (talk until switched off)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TalkControl:
    held: bool = False  # the talk key / button is down right now
    latched: bool = False  # "dauer": keeps talking after the key is released

    @property
    def transmitting(self) -> bool:
        return self.held or self.latched

    def press(self) -> None:
        self.held = True

    def release(self) -> None:
        self.held = False

    def toggle_latch(self) -> None:
        self.latched = not self.latched

    def stop_all(self) -> None:
        self.held = False
        self.latched = False
