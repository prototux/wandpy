"""
Base class for wand protocol drivers.

A driver translates the generic Wand API into one wand's BLE protocol and
reports incoming data back to the Wand. Users never instantiate drivers
directly; `Wand.connect()` picks the right one.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar, FrozenSet, List, Optional, Sequence, Tuple

from bleak import BleakClient

from wandpy.constants import Color, Feature, LedGroup, LedPattern, VibrationPattern, WandType
from wandpy.device import DeviceInfo
from wandpy.errors import UnsupportedFeatureError

if TYPE_CHECKING:
    from wandpy.macro import Macro
    from wandpy.wand import Wand

logger = logging.getLogger(__name__)


class WandDriver:
    """Protocol driver interface. Optional features raise UnsupportedFeatureError."""

    wand_type: ClassVar[WandType]
    features: ClassVar[FrozenSet[Feature]] = frozenset()
    service_uuid: ClassVar[str]

    # Seconds between keepalive packets, None if the wand doesn't need them
    default_keepalive_interval: ClassVar[Optional[float]] = None
    # Number of independent buttons/touch pads
    button_count: ClassVar[int] = 1

    def __init__(self, wand: "Wand", client: BleakClient) -> None:
        self.wand = wand
        self.client = client

    # ==================== Detection ====================

    @classmethod
    def matches_advertisement(cls, name: Optional[str], service_uuids: Sequence[str]) -> bool:
        """Whether a scanned device looks like this kind of wand."""
        return cls.service_uuid.lower() in (u.lower() for u in service_uuids)

    @classmethod
    def matches_client(cls, client: BleakClient) -> bool:
        """Whether a connected device exposes this wand's GATT service."""
        try:
            return client.services.get_service(cls.service_uuid) is not None
        except Exception:
            return False

    # ==================== Lifecycle ====================

    async def start(self) -> None:
        """Subscribe to notifications and initialize the wand."""

    async def stop(self) -> None:
        """Stop background work. The link may already be gone."""

    async def keepalive(self) -> None:
        """Send one keepalive packet."""

    async def read_device_info(self) -> DeviceInfo:
        return DeviceInfo(wand_type=self.wand_type)

    # ==================== Light ====================

    async def set_led(
        self,
        color: Color,
        pattern: LedPattern,
        group: Optional[LedGroup],
        transition_ms: int,
        duration_ms: Optional[int],
    ) -> None:
        raise UnsupportedFeatureError(Feature.LED, self.wand_type)

    async def clear_leds(self) -> None:
        raise UnsupportedFeatureError(Feature.LED, self.wand_type)

    # ==================== Haptics ====================

    async def vibrate(self, pattern: VibrationPattern) -> None:
        raise UnsupportedFeatureError(Feature.VIBRATION, self.wand_type)

    async def vibrate_for(self, duration_ms: int) -> None:
        raise UnsupportedFeatureError(Feature.VIBRATION_DURATION, self.wand_type)

    # ==================== IMU ====================

    async def reset_orientation(self) -> None:
        raise UnsupportedFeatureError(Feature.ORIENTATION_RESET, self.wand_type)

    async def calibrate_imu(self) -> bool:
        raise UnsupportedFeatureError(Feature.IMU_CALIBRATION, self.wand_type)

    async def calibrate_magnetometer(self) -> None:
        raise UnsupportedFeatureError(Feature.MAGNETOMETER_CALIBRATION, self.wand_type)

    async def start_imu(self) -> None:
        raise UnsupportedFeatureError(Feature.IMU_STREAM_CONTROL, self.wand_type)

    async def stop_imu(self) -> None:
        raise UnsupportedFeatureError(Feature.IMU_STREAM_CONTROL, self.wand_type)

    # ==================== Buttons ====================

    async def calibrate_buttons(self) -> bool:
        raise UnsupportedFeatureError(Feature.BUTTON_CALIBRATION, self.wand_type)

    async def get_button_thresholds(self) -> List[Tuple[int, int]]:
        raise UnsupportedFeatureError(Feature.BUTTON_THRESHOLDS, self.wand_type)

    async def set_button_thresholds(self, thresholds: Sequence[Tuple[int, int]]) -> None:
        raise UnsupportedFeatureError(Feature.BUTTON_THRESHOLDS, self.wand_type)

    # ==================== Macros ====================

    async def play_macro(self, macro: "Macro", replace: bool) -> None:
        raise UnsupportedFeatureError(Feature.MACROS, self.wand_type)

    async def stop_macro(self) -> None:
        raise UnsupportedFeatureError(Feature.MACROS, self.wand_type)
