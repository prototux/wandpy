"""
Driver for the Harry Potter Magic Caster Wand.

The Magic Caster wand talks through a single command characteristic and a
single notification characteristic; the first byte of every packet is an
opcode. Its IMU streams raw gyroscope/accelerometer batches (no on-device
fusion), it recognizes spells itself, has four capacitive touch pads, four
RGB LED groups and a macro engine for light/haptic sequences.

Protocol references:
- https://github.com/eigger/hass-magic-caster-wand (opcodes from the official APK)
- https://github.com/oelison/magic-caster-wand-open-source-controller
- https://github.com/whymaxwhy/Magic-Caster-Wand-Open-app-ai
- https://github.com/Dakewlmancoding/OpenMagicCasterWand
"""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import TYPE_CHECKING, Dict, Hashable, List, Optional, Sequence, Tuple

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic

from wandpy.constants import Color, Feature, LedGroup, LedPattern, VibrationPattern, WandType
from wandpy.device import DeviceInfo
from wandpy.drivers.base import WandDriver
from wandpy.errors import WandTimeoutError
from wandpy.fusion import MAGIC_CASTER_TO_KANO_AXES, OrientationFilter
from wandpy.imu import MAGIC_CASTER_IMU_RATE_HZ, QuaternionData, RawData
from wandpy.macro import Macro
from wandpy.spell import Spell
from wandpy.utils import spawn

if TYPE_CHECKING:
    from wandpy.wand import Wand

logger = logging.getLogger(__name__)


class MagicCasterUUIDs:
    """BLE GATT UUIDs used by the Magic Caster wand."""

    SERVICE = "57420001-587e-48a0-974c-544d6163c577"
    COMMAND = "57420002-587e-48a0-974c-544d6163c577"  # Write
    NOTIFY = "57420003-587e-48a0-974c-544d6163c577"  # Notify
    BATTERY = "00002a19-0000-1000-8000-00805f9b34fb"  # Standard battery level

    # The Magic Caster Box (charging case), not a wand
    BOX_SERVICE = "57420001-587e-48a0-974c-54686f72c577"


class Command:
    """Opcodes sent to the wand."""

    FIRMWARE_VERSION_READ = 0x00
    CHALLENGE = 0x01
    PAIR_WITH_ME = 0x03
    BOX_ADDRESS_READ = 0x09
    PRODUCT_INFO_READ = 0x0E
    IMU_STREAM_START = 0x30
    IMU_STREAM_STOP = 0x31
    LIGHT_CLEAR_ALL = 0x40
    LIGHT_SET = 0x42
    MACRO_FLUSH = 0x60
    MACRO_CONTROL = 0x68
    BUTTON_SET_THRESHOLD = 0xDC
    BUTTON_READ_THRESHOLD = 0xDD
    BUTTON_CALIBRATION_BASELINE = 0xFB
    IMU_CALIBRATION = 0xFC
    FACTORY_UNLOCK = 0xFE


class Response:
    """Opcodes received from the wand."""

    FIRMWARE_VERSION = 0x00
    CHALLENGE = 0x01
    PONG = 0x02
    BOX_ADDRESS = 0x09
    PRODUCT_INFO = 0x0E
    BUTTONS = 0x10
    SPELL = 0x24
    IMU = 0x2C
    BUTTON_THRESHOLD = 0xDD
    BUTTON_CALIBRATION_BASELINE = 0xFB
    IMU_CALIBRATION = 0xFC


class ProductInfo:
    """Sub-types of the product information request."""

    SERIAL_NUMBER = 0x01
    SKU = 0x02
    DEVICE_ID = 0x04


# Device ID suffix -> wand model (from the official app)
WAND_MODELS = {
    "DF": "DEFIANT",
    "LY": "LOYAL",
    "HR": "HEROIC",
    "HN": "HONOURABLE",
    "AV": "ADVENTUROUS",
    "WS": "WISE",
}

FACTORY_UNLOCK = bytes([Command.FACTORY_UNLOCK, 0x55, 0xAA])
GRIP_MASK = 0x0F  # All four pads: the "button" used to cast
PAD_COUNT = 4
CONTINUOUS_LOOPS = 255  # Longest loop a macro can express

# LedPattern emulation: half-period in ms
_PULSE_MS = {
    LedPattern.PULSE_FAST: 400,
    LedPattern.PULSE_SLOW: 1000,
    LedPattern.PULSE_EXTRASLOW: 2000,
}


def model_from_device_id(device_id: str) -> Optional[str]:
    """'WBMC22G1SDFW' -> 'DEFIANT' (suffix before the last character)."""
    if len(device_id) < 3:
        return None
    return WAND_MODELS.get(device_id[:-1][-2:])


def vibration_macro(pattern: VibrationPattern) -> Macro:
    """Reproduce a Kano vibration pattern with buzz steps."""
    m = Macro()
    if pattern == VibrationPattern.SHORT:
        return m.buzz(100)
    if pattern == VibrationPattern.LONG:
        return m.buzz(800)
    if pattern == VibrationPattern.BURST:
        return m.repeat(3, Macro().buzz(80).delay(150))
    if pattern == VibrationPattern.SHORT_LONG:
        return m.buzz(100).delay(250).buzz(600)
    if pattern == VibrationPattern.SHORT_SHORT:
        return m.buzz(100).delay(250).buzz(100)
    if pattern == VibrationPattern.PAUSE:
        return m.buzz(200).delay(500).buzz(200)
    return m.buzz(300)  # REGULAR


def led_pattern_macro(color: Color, pattern: LedPattern, group: LedGroup) -> Macro:
    """Reproduce a Kano LED pattern with light steps."""
    if pattern == LedPattern.BLINK:
        body = Macro().led(color, group).delay(500).led(Color.OFF, group).delay(500)
        return Macro().repeat(CONTINUOUS_LOOPS, body)
    if pattern == LedPattern.ERROR:
        body = Macro().led(color, group).delay(100).led(Color.OFF, group).delay(100)
        return Macro().repeat(5, body)
    if pattern == LedPattern.RGB:
        body = Macro()
        for c in (Color.RED, Color.GREEN, Color.BLUE):
            body.led(c, group, 500).wait()
        return Macro().repeat(CONTINUOUS_LOOPS, body)
    if pattern in _PULSE_MS:
        t = _PULSE_MS[pattern]
        body = Macro().led(color, group, t).wait().led(Color.OFF, group, t).wait()
        return Macro().repeat(CONTINUOUS_LOOPS, body)
    return Macro().led(color, group)  # FIXED


class MagicCasterDriver(WandDriver):
    """Protocol driver for the Harry Potter Magic Caster Wand."""

    wand_type = WandType.MAGIC_CASTER
    service_uuid = MagicCasterUUIDs.SERVICE
    default_keepalive_interval = None
    button_count = PAD_COUNT
    features = frozenset(
        {
            Feature.LED,
            Feature.LED_PATTERNS,
            Feature.LED_GROUPS,
            Feature.LED_TRANSITIONS,
            Feature.VIBRATION,
            Feature.VIBRATION_DURATION,
            Feature.BUTTON,
            Feature.TOUCH_PADS,
            Feature.BUTTON_THRESHOLDS,
            Feature.BUTTON_CALIBRATION,
            Feature.BATTERY,
            Feature.IMU_QUATERNIONS,
            Feature.IMU_RAW,
            Feature.IMU_STREAM_CONTROL,
            Feature.ORIENTATION_RESET,
            Feature.IMU_CALIBRATION,
            Feature.SPELLS,
            Feature.MACROS,
        }
    )

    request_timeout = 2.0

    def __init__(self, wand: "Wand", client: BleakClient) -> None:
        super().__init__(wand, client)
        self._write_lock = asyncio.Lock()
        self._pending: Dict[Hashable, asyncio.Future] = {}
        self._last_mask: Optional[int] = None
        self._imu_streaming = False
        self._next_sample_index: Optional[int] = None
        self.orientation = OrientationFilter(
            sample_rate=MAGIC_CASTER_IMU_RATE_HZ, axis_map=MAGIC_CASTER_TO_KANO_AXES
        )

    @classmethod
    def matches_advertisement(cls, name: Optional[str], service_uuids: Sequence[str]) -> bool:
        if super().matches_advertisement(name, service_uuids):
            return True
        return (name or "").upper().startswith("MCW")

    # ==================== Lifecycle ====================

    async def start(self) -> None:
        await self.client.start_notify(MagicCasterUUIDs.NOTIFY, self._handle_notification)
        try:
            await self.client.start_notify(MagicCasterUUIDs.BATTERY, self._handle_battery)
            self._handle_battery(None, await self.client.read_gatt_char(MagicCasterUUIDs.BATTERY))
        except Exception as e:
            logger.warning(f"Could not read battery level: {e}")

        self.wand._button.reset(0)

        if self.wand.button_thresholds is not None:
            await self.set_button_thresholds(self.wand.button_thresholds)

        if self.wand.imu_streaming:
            await self.start_imu()
        logger.info("All subscriptions active")

    async def stop(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()

        if self._imu_streaming and self.client.is_connected:
            try:
                await self._send(bytes([Command.IMU_STREAM_STOP]))
            except Exception as e:
                logger.debug(f"Could not stop IMU streaming: {e}")
        self._imu_streaming = False

    async def keepalive(self) -> None:
        await self._send(bytes([Command.CHALLENGE]))

    async def read_device_info(self) -> DeviceInfo:
        info = DeviceInfo(wand_type=self.wand_type)

        async def ask(packet: bytes, key: Hashable) -> Optional[bytes]:
            try:
                return await self._request(packet, key)
            except Exception as e:
                logger.debug(f"No answer to {packet.hex()}: {e}")
                return None

        data = await ask(bytes([Command.FIRMWARE_VERSION_READ]), Response.FIRMWARE_VERSION)
        if data and len(data) > 1:
            info.firmware_version = ".".join(str(b) for b in data[1:])

        data = await ask(
            bytes([Command.PRODUCT_INFO_READ, ProductInfo.SERIAL_NUMBER]),
            (Response.PRODUCT_INFO, ProductInfo.SERIAL_NUMBER),
        )
        if data and len(data) >= 6:
            info.serial_number = str(struct.unpack("<I", data[2:6])[0])

        data = await ask(
            bytes([Command.PRODUCT_INFO_READ, ProductInfo.SKU]), (Response.PRODUCT_INFO, ProductInfo.SKU)
        )
        if data:
            info.sku = bytes(data[2:]).decode("ascii", errors="ignore").strip("\x00 ") or None

        data = await ask(
            bytes([Command.PRODUCT_INFO_READ, ProductInfo.DEVICE_ID]),
            (Response.PRODUCT_INFO, ProductInfo.DEVICE_ID),
        )
        if data:
            info.device_id = bytes(data[2:]).decode("ascii", errors="ignore").strip("\x00 ") or None
            if info.device_id:
                info.model = model_from_device_id(info.device_id)

        data = await ask(bytes([Command.BOX_ADDRESS_READ]), Response.BOX_ADDRESS)
        if data and len(data) >= 7:
            info.extra["box_address"] = ":".join(f"{b:02X}" for b in reversed(data[1:7]))

        logger.info(f"Device: {info.model} {info.device_id} FW:{info.firmware_version}")
        return info

    # ==================== Transport ====================

    async def _send(self, packet: bytes) -> None:
        mtu = getattr(self.client, "mtu_size", None)
        if mtu and len(packet) > mtu - 3:
            logger.warning(f"Packet of {len(packet)} bytes exceeds the MTU ({mtu}); the wand may reject it")
        async with self._write_lock:
            logger.debug(f"Write {packet.hex()}")
            await self.client.write_gatt_char(MagicCasterUUIDs.COMMAND, packet, response=False)

    async def _request(self, packet: bytes, key: Hashable, timeout: Optional[float] = None) -> bytes:
        """Send `packet` and wait for the notification identified by `key`."""
        timeout = self.request_timeout if timeout is None else timeout
        future = asyncio.get_running_loop().create_future()
        self._pending[key] = future
        try:
            await self._send(packet)
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise WandTimeoutError(f"No answer from the wand to {packet.hex()}") from None
        finally:
            if self._pending.get(key) is future:
                del self._pending[key]

    def _resolve(self, key: Hashable, data: bytes) -> None:
        future = self._pending.get(key)
        if future and not future.done():
            future.set_result(data)

    # ==================== Notifications ====================

    def _handle_battery(self, sender: Optional[BleakGATTCharacteristic], data: bytearray) -> None:
        if len(data) >= 1:
            self.wand._emit_battery(data[0])

    def _handle_notification(self, sender: BleakGATTCharacteristic, data: bytearray) -> None:
        if not data:
            return
        data = bytes(data)
        opcode = data[0]
        try:
            if opcode == Response.IMU:
                self._parse_imu(data)
            elif opcode == Response.BUTTONS:
                self._parse_buttons(data)
            elif opcode == Response.SPELL:
                spell = Spell.parse(data)
                if spell is not None:
                    self.wand._emit_spell(spell)
            elif opcode in (Response.PRODUCT_INFO, Response.BUTTON_THRESHOLD) and len(data) >= 2:
                self._resolve((opcode, data[1]), data)
            else:
                if opcode not in self._pending:
                    logger.debug(f"Unhandled notification: {data.hex()}")
                self._resolve(opcode, data)
        except Exception as e:
            logger.error(f"Error handling notification {data.hex()}: {e}")

    def _parse_buttons(self, data: bytes) -> None:
        """[0x10][mask]: one bit per touch pad, bit 0 = pad 1 (the big one)."""
        if len(data) < 2:
            return
        mask = data[1] & 0x0F
        if mask == self._last_mask:
            return
        self._last_mask = mask

        self.wand._emit_buttons(tuple(bool(mask & (1 << i)) for i in range(PAD_COUNT)))
        grip = 1 if (mask & GRIP_MASK) == GRIP_MASK else 0
        spawn(self._feed_grip(grip))

    async def _feed_grip(self, value: int) -> None:
        async with self.wand._button.lock:
            await self.wand._process_button(value, from_poll=False)

    def _parse_imu(self, data: bytes) -> None:
        """
        [0x2C][start index u16 LE][count][count x 12 bytes]
        Each sample: gyro x, y, z, accel x, y, z as int16 LE.
        """
        if len(data) < 4:
            return
        start_index = struct.unpack_from("<H", data, 1)[0]
        count = data[3]
        if len(data) < 4 + count * 12:
            logger.debug(f"Truncated IMU packet: {len(data)} bytes for {count} samples")
            return

        # Account for lost packets in the first sample's time step
        period = self.orientation.sample_period
        dt = period
        if self._next_sample_index is not None:
            gap = (start_index - self._next_sample_index) & 0xFFFF
            if 0 < gap < MAGIC_CASTER_IMU_RATE_HZ:  # Skip absurd gaps (e.g. counter reset)
                dt = period * (gap + 1)
        self._next_sample_index = (start_index + count) & 0xFFFF

        for i in range(count):
            offset = 4 + i * 12
            sample = RawData(
                raw_bytes=data[offset : offset + 12],
                wand_type=WandType.MAGIC_CASTER,
                sample_index=(start_index + i) & 0xFFFF,
            )
            if not sample.valid:
                continue
            self.orientation.update(sample.gyro_rads, sample.accel_g, dt)
            dt = period
            self.wand._emit_raw(sample)

        if count:
            self.wand._emit_quaternions(QuaternionData.from_xyzw(*self.orientation.quaternion))

    # ==================== Light ====================

    async def set_led(
        self,
        color: Color,
        pattern: LedPattern,
        group: Optional[LedGroup],
        transition_ms: int,
        duration_ms: Optional[int],
    ) -> None:
        if pattern == LedPattern.OFF or (color.is_off and pattern == LedPattern.FIXED and not transition_ms):
            if group is None or group == LedGroup.ALL:
                await self.clear_leds()
            else:
                await self._send(bytes([Command.MACRO_FLUSH]))
                await self._set_groups(Color.OFF, group)
            return

        group = LedGroup.TIP if group is None else group

        # Replace whatever effect is currently playing, like on the Kano wand
        await self._send(bytes([Command.MACRO_FLUSH]))

        if pattern == LedPattern.FIXED and not transition_ms and not duration_ms:
            await self._set_groups(color, group)
            return

        if pattern == LedPattern.FIXED:
            macro = Macro().led(color, group, transition_ms)
            if duration_ms:
                macro.wait().delay(duration_ms).led(Color.OFF, group)
        else:
            if transition_ms:
                logger.debug("Transitions are ignored for LED patterns")
            macro = led_pattern_macro(color, pattern, group)
            if duration_ms:
                logger.debug("Duration is ignored for LED patterns")
        await self._send(macro.to_bytes())

    async def _set_groups(self, color: Color, group: LedGroup) -> None:
        for g in LedGroup.expand(group):
            await self._send(bytes([Command.LIGHT_SET, int(g), color.r, color.g, color.b]))

    async def clear_leds(self) -> None:
        await self._send(bytes([Command.MACRO_FLUSH]))
        await self._send(bytes([Command.LIGHT_CLEAR_ALL]))

    # ==================== Haptics ====================

    async def vibrate(self, pattern: VibrationPattern) -> None:
        await self._send(vibration_macro(pattern).to_bytes())

    async def vibrate_for(self, duration_ms: int) -> None:
        await self._send(Macro().buzz(duration_ms).to_bytes())

    # ==================== Macros ====================

    async def play_macro(self, macro: Macro, replace: bool) -> None:
        if replace:
            await self._send(bytes([Command.MACRO_FLUSH]))
        await self._send(macro.to_bytes())

    async def stop_macro(self) -> None:
        await self._send(bytes([Command.MACRO_FLUSH]))

    # ==================== IMU ====================

    async def start_imu(self) -> None:
        await self._send(bytes([Command.IMU_STREAM_STOP]))
        await asyncio.sleep(0.1)
        await self._send(bytes([Command.IMU_STREAM_START, 0x00, 0x80]))
        self._imu_streaming = True
        self._next_sample_index = None
        self.orientation.reset()

    async def stop_imu(self) -> None:
        await self._send(bytes([Command.IMU_STREAM_STOP]))
        self._imu_streaming = False

    @property
    def imu_streaming(self) -> bool:
        return self._imu_streaming

    async def reset_orientation(self) -> None:
        # Orientation is computed by WandPy: re-align on gravity and zero the yaw.
        self.orientation.reset()
        logger.info("Orientation reset")

    async def calibrate_imu(self) -> bool:
        await self._send(FACTORY_UNLOCK)
        await self._request(bytes([Command.IMU_CALIBRATION]), Response.IMU_CALIBRATION, timeout=10.0)
        self.orientation.gyro_bias = (0.0, 0.0, 0.0)
        self.orientation.reset()
        logger.info("IMU calibration done")
        return True

    # ==================== Buttons ====================

    async def calibrate_buttons(self) -> bool:
        await self._send(FACTORY_UNLOCK)
        await self._request(
            bytes([Command.BUTTON_CALIBRATION_BASELINE]), Response.BUTTON_CALIBRATION_BASELINE, timeout=10.0
        )
        logger.info("Touch pad calibration done")
        return True

    async def get_button_thresholds(self) -> List[Tuple[int, int]]:
        values = []
        for index in range(PAD_COUNT * 2):
            data = await self._request(
                bytes([Command.BUTTON_READ_THRESHOLD, index]), (Response.BUTTON_THRESHOLD, index)
            )
            values.append(data[2] if len(data) >= 3 else 0)
        return [(values[i], values[i + PAD_COUNT]) for i in range(PAD_COUNT)]

    async def set_button_thresholds(self, thresholds: Sequence[Tuple[int, int]]) -> None:
        if len(thresholds) != PAD_COUNT:
            raise ValueError(f"Expected {PAD_COUNT} (min, max) thresholds, got {len(thresholds)}")
        for pad, (low, high) in enumerate(thresholds):
            if not (0 <= low <= 255 and 0 <= high <= 255):
                raise ValueError(f"Thresholds must be in 0..255, got {(low, high)}")
            await self._send(bytes([Command.BUTTON_SET_THRESHOLD, pad, low]))
            await self._send(bytes([Command.BUTTON_SET_THRESHOLD, pad + PAD_COUNT, high]))
