"""
Protocol drivers, one per wand type.

`Wand` picks the right driver automatically; you only need this module to
inspect protocol details (UUIDs, opcodes) or to add support for a new wand.
"""

from __future__ import annotations

from typing import Optional, Sequence, Tuple, Type

from bleak import BleakClient

from wandpy.constants import WandType
from wandpy.drivers.base import WandDriver
from wandpy.drivers.kano import KanoDriver, KanoUUIDs
from wandpy.drivers.magic_caster import MagicCasterDriver, MagicCasterUUIDs

# Order matters for name-based detection: the most specific first.
DRIVERS: Tuple[Type[WandDriver], ...] = (MagicCasterDriver, KanoDriver)


def driver_for_type(wand_type: WandType) -> Type[WandDriver]:
    """Return the driver class for a wand type."""
    for driver in DRIVERS:
        if driver.wand_type == wand_type:
            return driver
    raise ValueError(f"No driver for wand type {wand_type!r}")


def detect_from_advertisement(name: Optional[str], service_uuids: Sequence[str]) -> Optional[WandType]:
    """Guess the wand type from scan data, None if it is not a wand."""
    if (name or "").upper().startswith("MCB") or MagicCasterUUIDs.BOX_SERVICE in (u.lower() for u in service_uuids):
        return None  # Magic Caster Box (charging case)
    for driver in DRIVERS:
        if driver.matches_advertisement(name, service_uuids):
            return driver.wand_type
    return None


def detect_from_client(client: BleakClient) -> Optional[WandType]:
    """Identify the wand type of a connected device from its GATT services."""
    for driver in DRIVERS:
        if driver.matches_client(client):
            return driver.wand_type
    return None


__all__ = [
    "DRIVERS",
    "WandDriver",
    "KanoDriver",
    "KanoUUIDs",
    "MagicCasterDriver",
    "MagicCasterUUIDs",
    "driver_for_type",
    "detect_from_advertisement",
    "detect_from_client",
]
