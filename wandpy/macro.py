"""
Macros: light and haptic sequences played by the Magic Caster wand itself.

A macro is a list of steps (fade an LED group, wait, buzz, loop...) sent in a
single BLE packet and executed by the wand's firmware, so timings are exact
regardless of your program or the Bluetooth link.

    >>> from wandpy import Macro, Color, LedGroup
    >>> flash = (
    ...     Macro()
    ...     .buzz(150)
    ...     .led(Color.GOLD, group=LedGroup.TIP, transition_ms=200)
    ...     .delay(400)
    ...     .clear()
    ... )
    >>> await wand.play_macro(flash)

    >>> # Loops
    >>> blink = Macro().repeat(5, Macro().led(Color.RED).delay(200).led(Color.OFF).delay(200))
"""

from __future__ import annotations

import struct
from enum import IntEnum
from typing import List, Union

from wandpy.constants import Color, LedGroup

MAX_DURATION_MS = 0xFFFF
MAX_LOOPS = 0xFF


class MacroOp(IntEnum):
    """Macro step opcodes (from the official app)."""

    DELAY = 0x10
    WAIT_BUSY = 0x11
    LIGHT_CLEAR_ALL = 0x20
    LIGHT_TRANSITION = 0x22
    HAP_BUZZ = 0x50
    FLUSH = 0x60
    CONTROL = 0x68  # Packet header: "run this macro"
    SET_LOOPS = 0x80  # Ends a loop block and sets its repeat count
    LOOP_START = 0x81


def _duration(value: Union[int, float], what: str) -> int:
    value = int(value)
    if not 0 <= value <= MAX_DURATION_MS:
        raise ValueError(f"{what} must be in 0..{MAX_DURATION_MS} ms, got {value}")
    return value


class Macro:
    """
    Fluent builder for Magic Caster macros.

    Every method returns the macro itself so calls can be chained.
    """

    def __init__(self) -> None:
        self._steps: List[bytes] = []

    # --- Light ---
    def led(
        self,
        color: Color,
        group: LedGroup = LedGroup.TIP,
        transition_ms: int = 0,
    ) -> "Macro":
        """Fade `group` to `color` over `transition_ms` (0 = instantly)."""
        duration = _duration(transition_ms, "transition_ms")
        for g in LedGroup.expand(group):
            self._steps.append(
                bytes([MacroOp.LIGHT_TRANSITION, int(g), color.r, color.g, color.b]) + struct.pack("<H", duration)
            )
        return self

    def clear(self) -> "Macro":
        """Turn every LED off."""
        self._steps.append(bytes([MacroOp.LIGHT_CLEAR_ALL]))
        return self

    # --- Haptics ---
    def buzz(self, duration_ms: int) -> "Macro":
        """Vibrate for `duration_ms`."""
        self._steps.append(bytes([MacroOp.HAP_BUZZ]) + struct.pack("<H", _duration(duration_ms, "duration_ms")))
        return self

    # --- Timing ---
    def delay(self, duration_ms: int) -> "Macro":
        """Wait `duration_ms` before the next step."""
        self._steps.append(bytes([MacroOp.DELAY]) + struct.pack("<H", _duration(duration_ms, "duration_ms")))
        return self

    def wait(self) -> "Macro":
        """Wait for running transitions/buzzes to finish."""
        self._steps.append(bytes([MacroOp.WAIT_BUSY]))
        return self

    # --- Loops ---
    def loop_start(self) -> "Macro":
        """Mark the start of a loop block (close it with `loop_end`)."""
        self._steps.append(bytes([MacroOp.LOOP_START]))
        return self

    def loop_end(self, count: int) -> "Macro":
        """Close the current loop block, repeating it `count` times."""
        if not 1 <= count <= MAX_LOOPS:
            raise ValueError(f"count must be in 1..{MAX_LOOPS}, got {count}")
        self._steps.append(bytes([MacroOp.SET_LOOPS, count]))
        return self

    def repeat(self, count: int, body: "Macro") -> "Macro":
        """Append `body` wrapped in a loop running `count` times."""
        self.loop_start()
        self._steps.extend(body._steps)
        return self.loop_end(count)

    def then(self, other: "Macro") -> "Macro":
        """Append all steps of `other`."""
        self._steps.extend(other._steps)
        return self

    # --- Encoding ---
    def to_bytes(self) -> bytes:
        """Encode the macro as the packet sent to the wand."""
        return bytes([MacroOp.CONTROL]) + b"".join(self._steps)

    def __len__(self) -> int:
        return len(self._steps)

    def __add__(self, other: "Macro") -> "Macro":
        result = Macro()
        result._steps = self._steps + other._steps
        return result

    def __repr__(self) -> str:
        return f"Macro({self.to_bytes().hex()})"
