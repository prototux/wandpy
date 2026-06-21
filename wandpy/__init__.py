"""
WandPy - A Python library for the Kano Coding Wand.

This library provides an easy-to-use interface for connecting to and controlling
a Kano Coding Wand with IMU sensor, RGB LED, vibration motor, and button.

Basic usage:
    >>> from wandpy import Wand, Color
    >>> wand = Wand()
    >>> await wand.scan()
    >>> await wand.connect("AA:BB:CC:DD:EE:FF")
    >>> await wand.set_led(Color.TEAL)
    >>> await wand.vibrate()
"""

from wandpy.button import ButtonEvent
from wandpy.constants import LedPattern, Color, VibrationPattern
from wandpy.wand import WandUUIDs, Wand

__version__ = "1.0.0"
__all__ = [
    "Wand",
    "LedPattern",
    "Color",
    "VibrationPattern",
    "ButtonEvent",
    "WandUUIDs",
]
