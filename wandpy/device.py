"""
Descriptions of wands: scan results and device information.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, NamedTuple, Optional

from wandpy.constants import WandType


class WandInfo(NamedTuple):
    """
    A wand found by `scan()`.

    Pass it straight to `connect()`; the wand type is then known without
    probing. It is a tuple, so `devices[0][0]` is still the address.
    """

    address: str
    name: str
    service_uuids: List[str]
    wand_type: WandType
    rssi: Optional[int] = None
    device: Optional[Any] = None  # bleak BLEDevice, makes connecting faster

    def __str__(self) -> str:
        rssi = f", {self.rssi} dBm" if self.rssi is not None else ""
        return f"{self.name} ({self.address}, {self.wand_type.value}{rssi})"


@dataclass
class DeviceInfo:
    """
    Static information about a connected wand.

    Fields a wand does not provide are None. Anything extra lands in `extra`.

    Attributes:
        wand_type: Which kind of wand this is
        name: Bluetooth name
        address: Bluetooth address
        manufacturer: Maker name (Kano)
        model: Wand model, e.g. "DEFIANT" or "LOYAL" (Magic Caster)
        firmware_version: Firmware version string (Magic Caster)
        hardware_version: Hardware description (Kano)
        serial_number: Serial number (Magic Caster)
        sku: Product SKU (Magic Caster)
        device_id: Product identifier, e.g. "WBMC22G1SDFW" (Magic Caster)
        extra: Any other raw information
    """

    wand_type: WandType
    name: Optional[str] = None
    address: Optional[str] = None
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    firmware_version: Optional[str] = None
    hardware_version: Optional[str] = None
    serial_number: Optional[str] = None
    sku: Optional[str] = None
    device_id: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)
