"""
Driver for the Kano Coding Wand.

The Kano wand exposes one GATT characteristic per function (LED, vibration,
button, quaternions...), does its own sensor fusion, and needs a keepalive
packet every few seconds.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from typing import TYPE_CHECKING, Optional, Sequence

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic

from wandpy.constants import Color, Feature, LedGroup, LedPattern, VibrationPattern, WandType
from wandpy.device import DeviceInfo
from wandpy.drivers.base import WandDriver
from wandpy.imu import FusedData, QuaternionData, RawData

if TYPE_CHECKING:
    from wandpy.wand import Wand

logger = logging.getLogger(__name__)


class KanoUUIDs:
    """
    BLE GATT characteristic and service UUIDs used by the Kano wand.

    You normally don't need to use these directly - the Wand class handles
    all communication internally. Exposed for advanced use cases.
    """

    # Standard BLE services
    COMMON_CONFIG = "00001800-0000-1000-8000-00805f9b34fb"
    GENERIC_SERVICE = "00001801-0000-1000-8000-00805f9b34fb"

    # Wand-specific services
    WAND_SOFTWARE_INFO = "64a70010-f691-4b93-a6f4-0968f5b648f8"
    WAND_DATA = "64a70011-f691-4b93-a6f4-0968f5b648f8"
    DFU_SERVICE = "0000fe59-0000-1000-8000-00805f9b34fb"

    # Characteristics
    MAKER_NAME = "64a7000b-f691-4b93-a6f4-0968f5b648f8"
    HARDWARE_DESC = "64a70001-f691-4b93-a6f4-0968f5b648f8"
    BATTERY = "64a70007-f691-4b93-a6f4-0968f5b648f8"
    VIBRATION = "64a70008-f691-4b93-a6f4-0968f5b648f8"
    RGB_LED = "64a70009-f691-4b93-a6f4-0968f5b648f8"
    BUTTON = "64a7000d-f691-4b93-a6f4-0968f5b648f8"
    KEEPALIVE = "64a7000f-f691-4b93-a6f4-0968f5b648f8"

    # IMU data characteristics
    IMU_QUATERNIONS = "64a70002-f691-4b93-a6f4-0968f5b648f8"
    RESET_QUATERNIONS = "64a70004-f691-4b93-a6f4-0968f5b648f8"
    IMU_RAW = "64a7000a-f691-4b93-a6f4-0968f5b648f8"
    IMU_FUSED = "64a7000c-f691-4b93-a6f4-0968f5b648f8"
    IMU_TEMPERATURE = "64a70014-f691-4b93-a6f4-0968f5b648f8"
    MAG_CALIBRATION = "64a70021-f691-4b93-a6f4-0968f5b648f8"


class KanoDriver(WandDriver):
    """Protocol driver for the Kano Coding Wand."""

    wand_type = WandType.KANO
    service_uuid = KanoUUIDs.WAND_SOFTWARE_INFO
    default_keepalive_interval = 3.0
    button_count = 1
    features = frozenset(
        {
            Feature.LED,
            Feature.LED_PATTERNS,
            Feature.VIBRATION,
            Feature.BUTTON,
            Feature.BATTERY,
            Feature.TEMPERATURE,
            Feature.IMU_QUATERNIONS,
            Feature.IMU_RAW,
            Feature.IMU_FUSED,
            Feature.MAGNETOMETER,
            Feature.IMU_STREAM_CONTROL,
            Feature.ORIENTATION_RESET,
            Feature.IMU_CALIBRATION,
            Feature.MAGNETOMETER_CALIBRATION,
        }
    )

    def __init__(self, wand: "Wand", client: BleakClient) -> None:
        super().__init__(wand, client)
        self._button_poll_task: Optional[asyncio.Task] = None
        self._button_batch_task: Optional[asyncio.Task] = None
        self._pending_button_value: Optional[int] = None
        self._button_batch_interval_ms = 2.0
        self._led_off_task: Optional[asyncio.Task] = None

    @classmethod
    def matches_advertisement(cls, name: Optional[str], service_uuids: Sequence[str]) -> bool:
        if super().matches_advertisement(name, service_uuids):
            return True
        lowered = (name or "").lower()
        return "kano" in lowered or ("wand" in lowered and not lowered.startswith(("mcw", "mcb")))

    # ==================== Lifecycle ====================

    async def start(self) -> None:
        """Subscribe to all notification characteristics."""
        if self.wand.imu_streaming:
            await self.start_imu()
        await self.client.start_notify(KanoUUIDs.IMU_TEMPERATURE, self._handle_temperature)

        # Battery and button (batched notification handler)
        await self.client.start_notify(KanoUUIDs.BATTERY, self._handle_battery)
        await self.client.start_notify(KanoUUIDs.BUTTON, self._handle_button_notification_batched)

        # Read initial button state
        button = self.wand._button
        try:
            initial_btn = await self.client.read_gatt_char(KanoUUIDs.BUTTON)
            async with button.lock:
                button.reset(initial_btn[0] if initial_btn else 0)
                button.state.last_poll_time = time.time()
            logger.info(f"Initial button state: {button.last_raw_value}")
        except Exception as e:
            logger.warning(f"Could not read initial button state: {e}")
            button.reset(0)

        try:
            self._parse_battery(await self.client.read_gatt_char(KanoUUIDs.BATTERY))
        except Exception as e:
            logger.warning(f"Could not read battery level: {e}")

        self._button_poll_task = asyncio.create_task(self._button_poll_loop())
        logger.info("All subscriptions active")

    async def stop(self) -> None:
        for task in (self._button_poll_task, self._button_batch_task, self._led_off_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._button_poll_task = self._button_batch_task = self._led_off_task = None

    def _imu_subscriptions(self):
        return (
            (KanoUUIDs.IMU_QUATERNIONS, self._handle_imu_quaternions),
            (KanoUUIDs.IMU_RAW, self._handle_imu_raw),
            (KanoUUIDs.IMU_FUSED, self._handle_imu_fused),
        )

    async def start_imu(self) -> None:
        for uuid, handler in self._imu_subscriptions():
            await self.client.start_notify(uuid, handler)

    async def stop_imu(self) -> None:
        for uuid, _ in self._imu_subscriptions():
            try:
                await self.client.stop_notify(uuid)
            except Exception as e:
                logger.debug(f"Could not unsubscribe from {uuid}: {e}")

    async def keepalive(self) -> None:
        await self.client.write_gatt_char(KanoUUIDs.KEEPALIVE, b"\x01", response=False)

    async def read_device_info(self) -> DeviceInfo:
        info = DeviceInfo(wand_type=self.wand_type)
        try:
            maker = await self.client.read_gatt_char(KanoUUIDs.MAKER_NAME)
            info.manufacturer = bytes(maker).decode("ascii", errors="ignore").strip("\x00 ") or None
        except Exception as e:
            logger.debug(f"Could not read maker name: {e}")
        try:
            hw = await self.client.read_gatt_char(KanoUUIDs.HARDWARE_DESC)
            info.hardware_version = bytes(hw).hex()
        except Exception as e:
            logger.debug(f"Could not read hardware description: {e}")
        logger.info(f"Device: {info.manufacturer} HW:{info.hardware_version}")
        return info

    # ==================== Button polling ====================

    async def _button_poll_loop(self) -> None:
        """
        Poll button state to catch missed notifications.

        BLE notifications can occasionally be lost. This loop reads the
        button characteristic periodically and corrects state if it disagrees
        with the notification-driven state.
        """
        button = self.wand._button
        while self.wand.is_connected:
            try:
                await asyncio.sleep(self.wand.button_poll_interval_ms / 1000.0)

                if not self.client.is_connected:
                    continue

                # Read current button state
                data = await self.client.read_gatt_char(KanoUUIDs.BUTTON)
                if len(data) < 1:
                    continue

                raw_value = data[0]
                current_time = time.time()

                async with button.lock:
                    button.state.last_poll_time = current_time

                    notif_age = current_time - button.state.last_notification_time
                    poll_pressed = raw_value != 0

                    # If no recent notification, trust the poll
                    if notif_age > (self.wand.button_poll_timeout_ms / 1000.0):
                        if poll_pressed != button.state.is_pressed:
                            logger.debug(
                                f"Poll correction: notification={button.state.is_pressed}, poll={poll_pressed}"
                            )
                            button.state.missed_notifications += 1
                            await self.wand._process_button(raw_value, from_poll=True)

                    # If poll disagrees even with recent notification, double-check
                    elif poll_pressed != button.state.is_pressed:
                        await asyncio.sleep(0.005)  # 5ms
                        data2 = await self.client.read_gatt_char(KanoUUIDs.BUTTON)
                        if len(data2) >= 1 and (data2[0] != 0) == poll_pressed:
                            logger.debug("Poll confirmed state mismatch, correcting")
                            await self.wand._process_button(raw_value, from_poll=True)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Button poll error: {e}")
                await asyncio.sleep(0.1)

    # ==================== Notification handlers ====================

    def _handle_button_notification_batched(self, sender: BleakGATTCharacteristic, data: bytearray) -> None:
        """Batch rapid notifications to prevent processing floods."""
        if len(data) < 1:
            return

        self._pending_button_value = data[0]

        if self._button_batch_task is None or self._button_batch_task.done():
            self._button_batch_task = asyncio.create_task(self._process_button_after_delay())

    async def _process_button_after_delay(self) -> None:
        """Wait for burst to settle, then process button value."""
        await asyncio.sleep(self._button_batch_interval_ms / 1000.0)

        if self._pending_button_value is not None:
            async with self.wand._button.lock:
                value = self._pending_button_value
                self._pending_button_value = None
                await self.wand._process_button(value, from_poll=False)

    def _handle_imu_quaternions(self, sender: BleakGATTCharacteristic, data: bytearray) -> None:
        if len(data) >= 8:
            q1, q2, q3, q4 = struct.unpack("<4h", data[:8])
            self.wand._emit_quaternions(QuaternionData(q1=q1, q2=q2, q3=q3, q4=q4, raw_bytes=bytes(data)))

    def _handle_imu_raw(self, sender: BleakGATTCharacteristic, data: bytearray) -> None:
        self.wand._emit_raw(RawData(raw_bytes=bytes(data), wand_type=WandType.KANO))

    def _handle_imu_fused(self, sender: BleakGATTCharacteristic, data: bytearray) -> None:
        self.wand._emit_fused(FusedData(raw_bytes=bytes(data)))

    def _handle_temperature(self, sender: BleakGATTCharacteristic, data: bytearray) -> None:
        if len(data) >= 2:
            self.wand._emit_temperature(struct.unpack("<h", data[:2])[0])

    def _handle_battery(self, sender: BleakGATTCharacteristic, data: bytearray) -> None:
        self._parse_battery(data)

    def _parse_battery(self, data: bytes) -> None:
        if len(data) >= 1:
            self.wand._emit_battery(data[0])

    # ==================== Device control ====================

    async def set_led(
        self,
        color: Color,
        pattern: LedPattern,
        group: Optional[LedGroup],
        transition_ms: int,
        duration_ms: Optional[int],
    ) -> None:
        # The Kano wand has a single LED: the group is irrelevant, and there
        # are no smooth transitions.
        if transition_ms:
            logger.debug("Kano wand has no LED transitions, setting color immediately")

        self._cancel_led_off()
        if color.is_off and pattern == LedPattern.FIXED:
            pattern = LedPattern.OFF

        # Convert between RGB888 and Kano's format (RGB565)
        rgb = color.to_rgb565()

        # Send message: [pattern, high_byte, low_byte]
        await self.client.write_gatt_char(KanoUUIDs.RGB_LED, struct.pack(">BH", pattern, rgb), response=True)
        logger.debug(f"LED set to RGB({color.r},{color.g},{color.b}) -> pattern={pattern}, raw=0x{rgb:04X}")

        if duration_ms and pattern != LedPattern.OFF:
            self._led_off_task = asyncio.create_task(self._led_off_after(duration_ms / 1000.0))

    async def clear_leds(self) -> None:
        self._cancel_led_off()
        await self.client.write_gatt_char(
            KanoUUIDs.RGB_LED, struct.pack(">BH", LedPattern.OFF, 0), response=True
        )

    def _cancel_led_off(self) -> None:
        if self._led_off_task and not self._led_off_task.done() and self._led_off_task is not asyncio.current_task():
            self._led_off_task.cancel()
        self._led_off_task = None

    async def _led_off_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            if self.client.is_connected:
                await self.client.write_gatt_char(
                    KanoUUIDs.RGB_LED, struct.pack(">BH", LedPattern.OFF, 0), response=True
                )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"Could not turn LED off: {e}")

    async def vibrate(self, pattern: VibrationPattern) -> None:
        await self.client.write_gatt_char(KanoUUIDs.VIBRATION, bytes([pattern]), response=True)
        logger.debug(f"Vibration pattern: {pattern.name}")

    async def vibrate_for(self, duration_ms: int) -> None:
        # No arbitrary durations on Kano: use the closest built-in pattern.
        if duration_ms <= 200:
            pattern = VibrationPattern.SHORT
        elif duration_ms <= 600:
            pattern = VibrationPattern.REGULAR
        else:
            pattern = VibrationPattern.LONG
        logger.debug(f"Kano wand has no vibration duration, using {pattern.name} for {duration_ms}ms")
        await self.vibrate(pattern)

    async def reset_orientation(self) -> None:
        await self.client.write_gatt_char(KanoUUIDs.RESET_QUATERNIONS, b"\x01", response=True)
        logger.info("Quaternions reset")

    async def calibrate_magnetometer(self) -> None:
        await self.client.write_gatt_char(KanoUUIDs.MAG_CALIBRATION, b"\x01", response=True)
        logger.info("Magnetometer calibration started")

    async def calibrate_imu(self) -> bool:
        # The magnetometer is the only user-calibratable sensor on the Kano wand.
        # It does not confirm completion, hence False.
        await self.calibrate_magnetometer()
        return False
