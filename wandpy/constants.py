"""
Constants, enums, and color definitions for WandPy.

This module contains all the fixed values you'll need when working with the wand:
- Vibration patterns for haptic feedback
- Button event types
- BLE characteristic UUIDs
- Predefined LED colors
"""

from dataclasses import dataclass
from enum import IntEnum

class VibrationPattern(IntEnum):
    """
    Built-in vibration patterns available on the wand.

    Usage:
        >>> from wandpy import VibrationPattern
        >>> await wand.vibrate(VibrationPattern.BURST)
    """

    REGULAR = 1  # Standard vibration
    SHORT = 2  # Quick pulse
    BURST = 3  # Rapid multiple pulses
    LONG = 4  # Extended vibration
    SHORT_LONG = 5  # Short pulse followed by long
    SHORT_SHORT = 6  # Two quick pulses
    PAUSE = 7  # Brief pause pattern

class LedPattern(IntEnum):
    """
    Built-in LED patterns available on the wand.
    """

    OFF = 0
    PULSE_FAST = 1
    FIXED = 2
    BLINK = 3
    PULSE_SLOW = 4
    PULSE_EXTRASLOW = 5
    ERROR = 6
    RGB = 7

@dataclass(frozen=True)
class Color:
    """
    Predefined LED colors and RGB constructor.

    You can use named colors or create your own:

        >>> from wandpy import Color
        >>> await wand.set_led(Color.TEAL)          # Named color
        >>> await wand.set_led(Color(255, 0, 128))  # Custom RGB

    Note: The wand uses 5-bit per channel color, so values are downscaled.
    Some color precision loss is expected.
    """

    r: int
    g: int
    b: int

    # Predefined colors
    OFF = None  # Will be set after class definition
    RED = None
    GREEN = None
    BLUE = None
    WHITE = None
    BLACK = None
    YELLOW = None
    CYAN = None
    MAGENTA = None
    ORANGE = None
    PURPLE = None
    TEAL = None
    PINK = None
    LIME = None
    GOLD = None
    SILVER = None

# Initialize color constants
Color.OFF = Color(0, 0, 0)
Color.BLACK = Color.OFF
Color.RED = Color(255, 0, 0)
Color.GREEN = Color(0, 255, 0)
Color.BLUE = Color(0, 0, 255)
Color.WHITE = Color(255, 255, 255)
Color.YELLOW = Color(255, 255, 0)
Color.CYAN = Color(0, 255, 255)
Color.MAGENTA = Color(255, 0, 255)
Color.ORANGE = Color(255, 165, 0)
Color.PURPLE = Color(128, 0, 128)
Color.TEAL = Color(0, 128, 128)
Color.PINK = Color(255, 192, 203)
Color.LIME = Color(50, 205, 50)
Color.GOLD = Color(255, 215, 0)
Color.SILVER = Color(192, 192, 192)
