"""
WandPy - A Python library for BLE magic wands.

Supported wands:
- Kano Coding Wand
- Harry Potter Magic Caster Wand

One `Wand` class drives both: the wand type is detected when you connect,
and the LED, vibration, button, battery and motion APIs are the same for
every wand. Wand-specific features (spells, touch pads, macros on the Magic
Caster; temperature, magnetometer and fused data on the Kano) live on the
same object - check them with `wand.supports(Feature.X)`.

Basic usage:
    >>> import wandpy
    >>> from wandpy import Color
    >>> wand = await wandpy.connect()      # Nearest wand, any type
    >>> await wand.set_led(Color.TEAL)
    >>> await wand.vibrate()
    >>> wand.on_spell = lambda spell: print(spell.name)
"""

from wandpy.button import ButtonEvent, ButtonState
from wandpy.constants import Color, Feature, LedGroup, LedPattern, VibrationPattern, WandType
from wandpy.device import DeviceInfo, WandInfo
from wandpy.drivers import KanoUUIDs, MagicCasterUUIDs
from wandpy.errors import NotConnectedError, UnsupportedFeatureError, WandError, WandTimeoutError
from wandpy.imu import FusedData, QuaternionData, RawData
from wandpy.macro import Macro
from wandpy.spell import Spell
from wandpy.utils import setup_logging
from wandpy.wand import KanoWand, MagicCasterWand, Wand, WandState, connect, scan

# Backward compatibility: the Kano UUIDs used to be the only ones
WandUUIDs = KanoUUIDs

__version__ = "1.0.0"
__all__ = [
    # Wands
    "Wand",
    "KanoWand",
    "MagicCasterWand",
    "WandState",
    "scan",
    "connect",
    # Types and constants
    "WandType",
    "Feature",
    "WandInfo",
    "DeviceInfo",
    "Color",
    "LedPattern",
    "LedGroup",
    "VibrationPattern",
    "ButtonEvent",
    "ButtonState",
    "Spell",
    "Macro",
    # Data
    "QuaternionData",
    "RawData",
    "FusedData",
    # Errors
    "WandError",
    "NotConnectedError",
    "UnsupportedFeatureError",
    "WandTimeoutError",
    # Advanced
    "KanoUUIDs",
    "MagicCasterUUIDs",
    "WandUUIDs",
    "setup_logging",
]
