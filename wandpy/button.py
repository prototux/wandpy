"""
Button state tracking for WandPy.

The wand button can emit press, release, and hold events. This module
handles debouncing, hold detection, and maintains a press history for
gesture recognition (like double-press).

The "button" is the Kano wand's button, or the full grip (all four touch
pads squeezed) on the Magic Caster wand - the same gesture its firmware uses
to start casting. Individual Magic Caster pads are reported separately via
`Wand.on_buttons`.

You normally don't interact with this directly - the Wand class provides
callbacks. This is exposed for advanced use cases.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Deque, Optional, Tuple

logger = logging.getLogger(__name__)


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
    def last_press_duration(self) -> float:
        """Duration of the most recent completed press (in seconds), 0.0 if none."""
        if self.press_history:
            return self.press_history[-1][1]
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


class ButtonTracker:
    """
    Debounced press/release/hold state machine shared by all wand types.

    Drivers feed it raw pressed/released values; it updates `state` and calls
    `emit(event, state)` on every transition and while the button is held.
    """

    def __init__(
        self,
        emit: Callable[[ButtonEvent, ButtonState], None],
        debounce_ms: float = 20.0,
        hold_delay_ms: float = 300.0,
        hold_interval_ms: float = 50.0,
    ) -> None:
        self.emit = emit
        self.debounce_ms = debounce_ms
        self.hold_delay_ms = hold_delay_ms
        self.hold_interval_ms = hold_interval_ms

        self.state = ButtonState()
        self.lock = asyncio.Lock()
        self._last_raw_value: Optional[int] = None
        self._debounce_until: float = 0.0
        self._hold_task: Optional[asyncio.Task] = None

    def reset(self, raw_value: int = 0) -> None:
        """Forget the current press (used on (re)connect)."""
        self._last_raw_value = raw_value
        self._debounce_until = 0.0
        self.state.is_pressed = raw_value != 0
        if self.state.is_pressed:
            self.state.last_pressed_time = time.time()

    @property
    def last_raw_value(self) -> Optional[int]:
        return self._last_raw_value

    async def process(self, raw_value: int, from_poll: bool = False) -> Optional[ButtonEvent]:
        """
        Process a raw button value (0 = released, anything else = pressed).

        Must be called with `lock` held. Returns the edge event, if any.
        """
        current_time = time.time()

        if not from_poll:
            self.state.last_notification_time = current_time

        # Debounce check
        if current_time < self._debounce_until:
            return None

        if raw_value == self._last_raw_value:
            return None

        is_pressed = raw_value != 0
        was_pressed = self.state.is_pressed
        event: Optional[ButtonEvent] = None

        if is_pressed and not was_pressed:
            # ---- RISING EDGE (press) ----
            self._debounce_until = current_time + self.debounce_ms / 1000.0
            self.state.is_pressed = True
            self.state.last_pressed_time = current_time
            self.state.press_count += 1

            await self._cancel_hold()
            self._hold_task = asyncio.create_task(self._hold_loop())
            event = ButtonEvent.PRESSED

        elif not is_pressed and was_pressed:
            # ---- FALLING EDGE (release) ----
            self._debounce_until = current_time + self.debounce_ms / 1000.0

            press_time = self.state.last_pressed_time or current_time
            duration = current_time - press_time

            self.state.is_pressed = False
            self.state.last_released_time = current_time
            self.state.press_history.append((press_time, duration))

            await self._cancel_hold()
            event = ButtonEvent.RELEASED

        self._last_raw_value = raw_value
        if event is not None:
            self.emit(event, self.state)
        return event

    async def close(self) -> None:
        """Stop the hold task."""
        await self._cancel_hold()

    async def _cancel_hold(self) -> None:
        task, self._hold_task = self._hold_task, None
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _hold_loop(self) -> None:
        """Emit HOLD events while button is pressed."""
        try:
            await asyncio.sleep(self.hold_delay_ms / 1000.0)  # Initial delay before first HOLD
            while self.state.is_pressed:
                self.emit(ButtonEvent.HOLD, self.state)
                await asyncio.sleep(self.hold_interval_ms / 1000.0)
        except asyncio.CancelledError:
            pass
