"""
Constants, enums, and color definitions for WandPy.

This module contains all the fixed values you'll need when working with a wand:
- The supported wand types and their features
- Vibration patterns for haptic feedback
- LED patterns and LED groups
- Predefined LED colors
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import ClassVar


class WandType(str, Enum):
    """
    The kinds of wand supported by WandPy.

    You rarely need this: the wand type is detected automatically when you
    connect. It is useful to filter scans or to force a type.

        >>> wand = Wand(wand_type=WandType.MAGIC_CASTER)
    """

    KANO = "kano"  # Kano Coding Wand
    MAGIC_CASTER = "magic_caster"  # Harry Potter Magic Caster Wand

    def __str__(self) -> str:
        return self.value


class Feature(str, Enum):
    """
    Capabilities a wand may have.

    Check them with `wand.supports(Feature.SPELLS)` or look at `wand.features`.
    Calling a method that needs a feature the connected wand lacks raises
    UnsupportedFeatureError.
    """

    # Light
    LED = "led"  # Set the LED color
    LED_PATTERNS = "led_patterns"  # LedPattern effects (native on Kano, emulated with macros on Magic Caster)
    LED_GROUPS = "led_groups"  # Several independently addressable LEDs (LedGroup)
    LED_TRANSITIONS = "led_transitions"  # Smooth fades between colors

    # Haptics
    VIBRATION = "vibration"  # VibrationPattern (native on Kano, emulated with macros on Magic Caster)
    VIBRATION_DURATION = "vibration_duration"  # Vibrate for an arbitrary duration

    # Input
    BUTTON = "button"  # Primary button (Kano button, Magic Caster full grip)
    TOUCH_PADS = "touch_pads"  # Several independent capacitive pads
    BUTTON_THRESHOLDS = "button_thresholds"  # Read/write the pad sensitivity
    BUTTON_CALIBRATION = "button_calibration"  # Recalibrate the pad baseline

    # Sensors
    BATTERY = "battery"
    TEMPERATURE = "temperature"
    IMU_QUATERNIONS = "imu_quaternions"  # Orientation (on-device on Kano, computed by WandPy on Magic Caster)
    IMU_RAW = "imu_raw"  # Raw accelerometer/gyroscope (and magnetometer on Kano)
    IMU_FUSED = "imu_fused"  # Kano's on-device fused/filtered output
    MAGNETOMETER = "magnetometer"  # Absolute heading
    IMU_STREAM_CONTROL = "imu_stream_control"  # Start/stop IMU streaming to save battery
    ORIENTATION_RESET = "orientation_reset"  # Zero the current orientation
    IMU_CALIBRATION = "imu_calibration"
    MAGNETOMETER_CALIBRATION = "magnetometer_calibration"

    # Magic
    SPELLS = "spells"  # On-device spell recognition
    MACROS = "macros"  # On-device light/haptic sequences (Macro)

    def __str__(self) -> str:
        return self.value


class VibrationPattern(IntEnum):
    """
    Built-in vibration patterns.

    These are native on the Kano wand and reproduced with buzz macros on the
    Magic Caster wand, so the same code works on both.

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
    Built-in LED patterns.

    These are native on the Kano wand and reproduced with light macros on the
    Magic Caster wand, so the same code works on both.
    """

    OFF = 0
    PULSE_FAST = 1
    FIXED = 2
    BLINK = 3
    PULSE_SLOW = 4
    PULSE_EXTRASLOW = 5
    ERROR = 6
    RGB = 7


class LedGroup(IntEnum):
    """
    LED groups of the Magic Caster wand, from tip to pommel.

    The Kano wand has a single LED, so the group is ignored there.

        >>> await wand.set_led(Color.BLUE, group=LedGroup.POMMEL)
        >>> await wand.set_led(Color.RED, group=LedGroup.ALL)
    """

    TIP = 0
    MID_UPPER = 1
    MID_LOWER = 2
    POMMEL = 3
    ALL = 0xFF  # Every group at once (expanded by WandPy, not a wire value)

    @classmethod
    def expand(cls, group: "LedGroup") -> tuple:
        """Return the physical groups covered by `group`."""
        if group == cls.ALL:
            return (cls.TIP, cls.MID_UPPER, cls.MID_LOWER, cls.POMMEL)
        return (cls(group),)


@dataclass(frozen=True)
class Color:
    """
    Predefined LED colors and RGB constructor.

    You can use named colors or create your own:

        >>> from wandpy import Color
        >>> await wand.set_led(Color.TEAL)          # Named color
        >>> await wand.set_led(Color(255, 0, 128))  # Custom RGB
        >>> await wand.set_led(Color.from_hex("#ff8800"))

    Note: The Kano wand uses RGB565, so values are downscaled there and some
    color precision loss is expected. The Magic Caster wand uses full RGB888.
    """

    r: int
    g: int
    b: int

    # Predefined colors (set after the class definition)
    OFF: ClassVar["Color"]
    BLACK: ClassVar["Color"]
    RED: ClassVar["Color"]
    GREEN: ClassVar["Color"]
    BLUE: ClassVar["Color"]
    WHITE: ClassVar["Color"]
    YELLOW: ClassVar["Color"]
    CYAN: ClassVar["Color"]
    MAGENTA: ClassVar["Color"]
    ORANGE: ClassVar["Color"]
    PURPLE: ClassVar["Color"]
    TEAL: ClassVar["Color"]
    PINK: ClassVar["Color"]
    LIME: ClassVar["Color"]
    GOLD: ClassVar["Color"]
    SILVER: ClassVar["Color"]

    def __post_init__(self) -> None:
        for name in ("r", "g", "b"):
            value = getattr(self, name)
            if not 0 <= value <= 255:
                raise ValueError(f"Color.{name} must be in 0..255, got {value}")

    @classmethod
    def from_hex(cls, value: str) -> "Color":
        """Create a color from a hex string like '#FF8800' or 'f80'."""
        value = value.strip().lstrip("#")
        if len(value) == 3:
            value = "".join(c * 2 for c in value)
        if len(value) != 6:
            raise ValueError(f"Invalid hex color: {value!r}")
        return cls(int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))

    @property
    def is_off(self) -> bool:
        """True for pure black, which turns the LED off."""
        return self.r == 0 and self.g == 0 and self.b == 0

    def to_hex(self) -> str:
        """Return the color as '#RRGGBB'."""
        return f"#{self.r:02X}{self.g:02X}{self.b:02X}"

    def to_rgb565(self) -> int:
        """Return the color packed as RGB565 (the Kano wire format)."""
        return (self.r >> 3) << 11 | (self.g >> 2) << 5 | self.b >> 3


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
