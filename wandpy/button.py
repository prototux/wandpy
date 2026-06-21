"""
Button state tracking for WandPy.

The wand button can emit press, release, and hold events. This module
handles debouncing, hold detection, and maintains a press history for
gesture recognition (like double-press).

You normally don't interact with this directly - the Wand class provides
callbacks. This is exposed for advanced use cases.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional, Tuple
from enum import IntEnum

class ButtonEvent(IntEnum):
    """
    Types of button events emitted by the wand.

    The wand button supports three event types:
    - PRESSED: Button was just pressed down
    - RELEASED: Button was just released
    - HOLD: Button is being held down (fires repeatedly after initial press)

    Usage:
        >>> def on_button(event, state):
        ...     if event == ButtonEvent.PRESSED:
        ...         print("Button pressed!")
        >>> wand.on_button_event = on_button
    """

    PRESSED = 1
    RELEASED = 2
    HOLD = 3

@dataclass
class ButtonState:
    """
    Current and historical state of the wand button.

    Attributes:
        is_pressed: Whether the button is currently held down
        last_pressed_time: Timestamp of the most recent press
        last_released_time: Timestamp of the most recent release
        press_count: Total number of presses since connection
        press_history: Recent (time, duration) pairs for gesture detection
        press_duration: Current press duration in seconds (0 if not pressed)
        time_since_release: Seconds since last release (inf if never released)
    """

    is_pressed: bool = False
    last_pressed_time: Optional[float] = None
    last_released_time: Optional[float] = None
    press_count: int = 0

    # History of (press_time, duration) for double-press detection
    press_history: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=10))

    # Internal tracking for notification reliability
    last_notification_time: float = 0.0
    last_poll_time: float = 0.0
    missed_notifications: int = 0

    @property
    def press_duration(self) -> float:
        """
        How long the button has been held (in seconds).

        Returns 0.0 if the button is not currently pressed.
        """
        if self.is_pressed and self.last_pressed_time:
            return time.time() - self.last_pressed_time
        return 0.0

    @property
    def time_since_release(self) -> float:
        """
        Time since the button was last released (in seconds).

        Returns infinity if the button has never been released.
        """
        if self.last_released_time:
            return time.time() - self.last_released_time
        return float("inf")

    def was_double_press(self, window_ms: float = 500.0) -> bool:
        """
        Check if the last two presses were a double-press.

        Args:
            window_ms: Maximum milliseconds between presses to count as double-press

        Returns:
            True if the last two presses occurred within the window.
        """
        if len(self.press_history) < 2:
            return False
        t1, _ = self.press_history[-2]
        t2, _ = self.press_history[-1]
        return (t2 - t1) * 1000 < window_ms
