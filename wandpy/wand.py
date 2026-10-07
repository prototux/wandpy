"""
Main Wand class for WandPy.

This is the primary interface for interacting with the BLE wand.
Connect to a wand, receive sensor data, control the LED and vibration motor.

Basic usage:
    >>> from wandpy import Wand, Color, VibrationPattern
    >>> wand = Wand()
    >>> await wand.connect("AA:BB:CC:DD:EE:FF")
    >>> await wand.set_led(Color.TEAL)
    >>> await wand.vibrate(VibrationPattern.SHORT)

Callbacks for sensor data:
    >>> def on_imu(data):
    ...     euler = data.to_euler_angles()
    ...     print(f"Yaw: {euler['yaw']:.1f}")
    >>> wand.on_imu_quaternions = on_imu
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic

from wandpy.button import ButtonEvent, ButtonState
from wandpy.constants import Color, LedPattern, VibrationPattern
from wandpy.imu import FusedData, QuaternionData, RawData

logger = logging.getLogger(__name__)

class WandUUIDs:
    """
    BLE GATT characteristic and service UUIDs used by the wand.

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

class WandState:
    """
    Snapshot of the wand's current state.

    Attributes:
        connected: Whether the wand is currently connected
        battery: Battery level percentage (0-100), or None if unknown
        temperature: Temperature in degrees (device units), or None
        button_pressed: Whether the button is currently pressed
        imu_quaternions: Latest quaternion data, or None
        imu_raw: Latest raw 9-axis data, or None
        imu_fused: Latest filtered linear acceleration data, or None
        last_update: Timestamp of the most recent data update
    """

    def __init__(self):
        self.connected: bool = False
        self.battery: Optional[int] = None
        self.temperature: Optional[float] = None
        self.button_pressed: bool = False
        self.imu_quaternions: Optional[QuaternionData] = None
        self.imu_raw: Optional[RawData] = None
        self.imu_fused: Optional[FusedData] = None
        self.last_update: float = time.time()


class Wand:
    """
    Main class for controlling a BLE wand device.

    This class handles connection management, sensor data streaming,
    LED control, vibration, and button input.

    Example:
        >>> wand = Wand()
        >>> devices = await wand.scan()
        >>> await wand.connect(devices[0][0])
        >>>
        >>> # Set up callbacks
        >>> wand.on_imu_quaternions = lambda q: print(q.to_euler_angles())
        >>> wand.on_button_event = lambda e, s: print(f"Button: {e.name}")
        >>>
        >>> await wand.set_led(Color.PURPLE)
        >>> await wand.vibrate()
    """

    def __init__(
        self,
        mac_address: Optional[str] = None,
        keepalive_interval: float = 3.0,
        auto_reconnect: bool = True,
        button_debounce_ms: float = 20.0,
        button_hold_interval_ms: float = 50.0,
        button_poll_interval_ms: float = 15.0,
        button_poll_timeout_ms: float = 100.0,
    ):
        """
        Create a new Wand instance.

        Args:
            mac_address: Optional MAC address to connect to. If not provided,
                         you must pass it to connect() or use scan() first.
            keepalive_interval: Seconds between keepalive packets (default: 3.0)
            auto_reconnect: Automatically reconnect on unexpected disconnect
            button_debounce_ms: Minimum time between button state changes (default: 20)
            button_hold_interval_ms: Milliseconds between HOLD events (default: 50)
            button_poll_interval_ms: Button polling frequency in ms (default: 15)
            button_poll_timeout_ms: When to trust poll over notifications (default: 100)
        """
        self.mac_address = mac_address
        self.keepalive_interval = keepalive_interval
        self.auto_reconnect = auto_reconnect

        # BLE client
        self._client: Optional[BleakClient] = None
        self._connected = False
        self._closing = False
        self._keepalive_task: Optional[asyncio.Task] = None

        # Connection tuning
        self._preferred_mtu = 512

        # State tracking
        self.state = WandState()
        self._history: Dict[str, Deque[Tuple[float, Any]]] = {
            "imu_quaternions": deque(maxlen=1000),
            "imu_raw": deque(maxlen=1000),
            "imu_fused": deque(maxlen=1000),
            "battery": deque(maxlen=100),
            "temperature": deque(maxlen=100),
        }

        # Button configuration
        self.button_debounce_ms = button_debounce_ms
        self.button_hold_interval_ms = button_hold_interval_ms
        self.button_poll_interval_ms = button_poll_interval_ms
        self.button_poll_timeout_ms = button_poll_timeout_ms

        # Button state machine
        self.button_state = ButtonState()
        self._last_button_raw_value: Optional[int] = None
        self._button_debounce_until: float = 0.0
        self._button_hold_task: Optional[asyncio.Task] = None
        self._button_lock = asyncio.Lock()

        # Button polling
        self._button_poll_task: Optional[asyncio.Task] = None
        self._button_batch_task: Optional[asyncio.Task] = None
        self._pending_button_value: Optional[int] = None
        self._button_batch_interval_ms = 2.0

        # --- User callbacks ---
        # Called with (bool) when button is pressed/released (legacy)
        self.on_button: Optional[Callable[[bool], None]] = None

        # Called with (ButtonEvent, ButtonState) for detailed button events
        self.on_button_event: Optional[Callable[[ButtonEvent, ButtonState], None]] = (None)

        # IMU data callbacks - receive the data type objects
        self.on_imu_quaternions: Optional[Callable[[QuaternionData], None]] = None
        self.on_imu_raw: Optional[Callable[[RawData], None]] = None
        self.on_imu_fused: Optional[Callable[[FusedData], None]] = None

        # Other sensor callbacks
        self.on_battery: Optional[Callable[[int], None]] = None
        self.on_temperature: Optional[Callable[[float], None]] = None

        # Connection callback
        self.on_disconnect: Optional[Callable[[], None]] = None

    # ==================== Connection ====================

    async def scan(self, timeout: float = 5.0) -> List[Tuple[str, str, List[str]]]:
        """
        Scan for nearby wand devices.

        Args:
            timeout: How long to scan in seconds

        Returns:
            List of (address, name, service_uuids) tuples for each found wand
        """
        discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
        wands = []

        for address, (device, advertisement) in discovered.items():
            is_wand = False
            service_uuids = []

            if device.name and "wand" in device.name.lower():
                is_wand = True

            if advertisement.service_uuids:
                service_uuids = advertisement.service_uuids
                for u in service_uuids:
                    if u.lower() == WandUUIDs.WAND_SOFTWARE_INFO.lower():
                        is_wand = True

            if is_wand:
                wands.append((device.address, device.name or "Unknown Wand", service_uuids))

        return wands

    async def connect(self, mac_address: Optional[str] = None) -> bool:
        """
        Connect to a wand device.

        Args:
            mac_address: Device address. Uses the one from __init__ if not provided.

        Returns:
            True on successful connection, False otherwise

        Raises:
            ValueError: If no MAC address is available
        """
        target_mac = mac_address or self.mac_address
        if not target_mac:
            raise ValueError("No MAC address provided. Use scan() to find devices or provide mac_address to __init__ or connect().")

        self.mac_address = target_mac
        self._closing = False

        try:
            # Create BLE client with disconnect callback
            self._client = BleakClient(target_mac, disconnected_callback=self._on_disconnect, mtu=self._preferred_mtu)

            await self._client.connect()
            await self._optimize_connection_params()

            if not self._client.is_connected:
                logger.error("Failed to connect")
                return False

            self._connected = True
            self.state.connected = True
            logger.info(f"Connected to {target_mac}")

            # Set up all notification subscriptions
            await self._setup_subscriptions()

            # Start background tasks
            self._start_keepalive()
            self._start_button_polling()

            # Read initial device info
            await self._read_initial_state()

            return True

        except Exception as e:
            logger.error(f"Connection error: {e}")
            self._connected = False
            self.state.connected = False
            return False

    async def disconnect(self) -> None:
        """Disconnect from the wand and clean up all background tasks."""
        self._closing = True
        self._connected = False
        self.state.connected = False

        # Cancel all background tasks
        for task in (self._button_poll_task, self._button_batch_task, self._button_hold_task, self._keepalive_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self._client and self._client.is_connected:
            await self._client.disconnect()
            logger.info("Disconnected")

    @property
    def is_connected(self) -> bool:
        """Whether the wand is currently connected."""
        return (self._connected and self._client is not None and self._client.is_connected)

    # ==================== Internal: Connection management ====================
    async def _optimize_connection_params(self) -> None:
        """Try to negotiate low-latency BLE connection parameters."""
        try:
            if hasattr(self._client, "mtu_size"):
                actual_mtu = self._client.mtu_size
                logger.info(f"Negotiated MTU: {actual_mtu}")

            # Platform-specific low-latency request (may not be available)
            if hasattr(self._client, "_request_connection_params"):
                # 7.5ms interval, 0 latency, 100ms timeout
                await self._client._request_connection_params(6, 6, 0, 100)
                logger.info("Requested low-latency connection parameters")
        except Exception as e:
            logger.debug(f"Could not optimize connection params: {e}")

    def _on_disconnect(self, client: BleakClient) -> None:
        """Handle unexpected disconnection."""
        if self._closing:
            return  # disconnect() was called, nothing unexpected

        logger.warning("Device disconnected unexpectedly")
        self._connected = False
        self.state.connected = False

        if self.on_disconnect:
            try:
                self.on_disconnect()
            except Exception as e:
                logger.error(f"Error in disconnect callback: {e}")

        if self.auto_reconnect:
            asyncio.create_task(self._attempt_reconnect())

    async def _attempt_reconnect(self) -> None:
        """Attempt to reconnect with exponential backoff."""
        for attempt in range(5):
            wait_time = min(2**attempt, 30)
            logger.info(f"Reconnecting in {wait_time}s... (attempt {attempt + 1})")
            await asyncio.sleep(wait_time)
            if self._closing:
                return

            if await self.connect():
                logger.info("Reconnected successfully")
                return

        logger.error("Failed to reconnect after 5 attempts")

    # ==================== Internal: BLE subscriptions ====================

    async def _setup_subscriptions(self) -> None:
        """Subscribe to all notification characteristics."""
        # IMU quaternions, raw and fused data + temperature
        await self._client.start_notify(WandUUIDs.IMU_QUATERNIONS, self._handle_imu_quaternions)
        await self._client.start_notify(WandUUIDs.IMU_RAW, self._handle_imu_raw)
        await self._client.start_notify(WandUUIDs.IMU_FUSED, self._handle_imu_fused)
        await self._client.start_notify(WandUUIDs.IMU_TEMPERATURE, self._handle_temperature)

        # Battery and button (batched notification handler)
        await self._client.start_notify(WandUUIDs.BATTERY, self._handle_battery)
        await self._client.start_notify(WandUUIDs.BUTTON, self._handle_button_notification_batched)

        # Read initial button state
        try:
            initial_btn = await self._client.read_gatt_char(WandUUIDs.BUTTON)
            async with self._button_lock:
                self._last_button_raw_value = initial_btn[0] if initial_btn else 0
                self.button_state.is_pressed = self._last_button_raw_value != 0
                self.button_state.last_poll_time = time.time()
            logger.info(f"Initial button state: {self._last_button_raw_value}")
        except Exception as e:
            logger.warning(f"Could not read initial button state: {e}")
            self._last_button_raw_value = 0

        logger.info("All subscriptions active")

    async def _read_initial_state(self) -> None:
        """Read device info on connect."""
        try:
            batt = await self._client.read_gatt_char(WandUUIDs.BATTERY)
            self._parse_battery_data(batt)

            maker = await self._client.read_gatt_char(WandUUIDs.MAKER_NAME)
            hw = await self._client.read_gatt_char(WandUUIDs.HARDWARE_DESC)
            logger.info(f"Device: {maker.decode('ascii', errors='ignore')} HW:{hw.hex()}")
        except Exception as e:
            logger.warning(f"Error reading initial state: {e}")

    # ==================== Internal: Keepalive ====================
    def _start_keepalive(self) -> None:
        """Start the keepalive background task."""
        if self._keepalive_task:
            self._keepalive_task.cancel()
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def _keepalive_loop(self) -> None:
        """Send periodic keepalive packets to maintain connection."""
        while self._connected:
            try:
                if self._client and self._client.is_connected:
                    await self._client.write_gatt_char(WandUUIDs.KEEPALIVE, b"\x01", response=False)
                await asyncio.sleep(self.keepalive_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Keepalive error: {e}")
                await asyncio.sleep(1)

    # ==================== Internal: Button polling ====================
    def _start_button_polling(self) -> None:
        """Start polling-based button verification for reliability."""
        if self._button_poll_task:
            self._button_poll_task.cancel()
        self._button_poll_task = asyncio.create_task(self._button_poll_loop())

    async def _button_poll_loop(self) -> None:
        """
        Poll button state to catch missed notifications.

        BLE notifications can occasionally be lost. This loop reads the
        button characteristic periodically and corrects state if it disagrees
        with the notification-driven state.
        """
        while self._connected:
            try:
                await asyncio.sleep(self.button_poll_interval_ms / 1000.0)

                if not self._client or not self._client.is_connected:
                    continue

                # Read current button state
                data = await self._client.read_gatt_char(WandUUIDs.BUTTON)
                if len(data) < 1:
                    continue

                raw_value = data[0]
                current_time = time.time()

                async with self._button_lock:
                    self.button_state.last_poll_time = current_time

                    notif_age = current_time - self.button_state.last_notification_time
                    poll_pressed = raw_value != 0

                    # If no recent notification, trust the poll
                    if notif_age > (self.button_poll_timeout_ms / 1000.0):
                        if poll_pressed != self.button_state.is_pressed:
                            logger.debug(f"Poll correction: notification={self.button_state.is_pressed}, poll={poll_pressed}")
                            self.button_state.missed_notifications += 1
                            await self._process_button_value(raw_value, from_poll=True)

                    # If poll disagrees even with recent notification, double-check
                    elif poll_pressed != self.button_state.is_pressed:
                        await asyncio.sleep(0.005)  # 5ms
                        data2 = await self._client.read_gatt_char(WandUUIDs.BUTTON)
                        if len(data2) >= 1 and (data2[0] != 0) == poll_pressed:
                            logger.debug("Poll confirmed state mismatch, correcting")
                            await self._process_button_value(raw_value, from_poll=True)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Button poll error: {e}")
                await asyncio.sleep(0.1)

    # ==================== Internal: Button handling ====================

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
            async with self._button_lock:
                value = self._pending_button_value
                self._pending_button_value = None
                await self._process_button_value(value, from_poll=False)

    async def _process_button_value(self, raw_value: int, from_poll: bool = False) -> None:
        """Process button value with proper state machine and debouncing."""
        current_time = time.time()

        if not from_poll:
            self.button_state.last_notification_time = current_time

        # Debounce check
        if current_time < self._button_debounce_until:
            return

        if raw_value == self._last_button_raw_value:
            return

        is_pressed = raw_value != 0
        was_pressed = self.button_state.is_pressed

        if is_pressed and not was_pressed:
            # ---- RISING EDGE (press) ----
            self._button_debounce_until = current_time + (
                self.button_debounce_ms / 1000.0
            )
            self.button_state.is_pressed = True
            self.button_state.last_pressed_time = current_time
            self.button_state.press_count += 1

            # Cancel any existing hold task
            if self._button_hold_task and not self._button_hold_task.done():
                self._button_hold_task.cancel()
                try:
                    await self._button_hold_task
                except asyncio.CancelledError:
                    pass

            # Start hold detection
            self._button_hold_task = asyncio.create_task(self._button_hold_loop())

            self._emit_button_event(ButtonEvent.PRESSED, self.button_state)
            if self.on_button:
                try:
                    self.on_button(True)
                except Exception as e:
                    logger.error(f"Error in button callback: {e}")

        elif not is_pressed and was_pressed:
            # ---- FALLING EDGE (release) ----
            self._button_debounce_until = current_time + (self.button_debounce_ms / 1000.0)

            press_time = self.button_state.last_pressed_time or current_time
            duration = current_time - press_time

            self.button_state.is_pressed = False
            self.button_state.last_released_time = current_time
            self.button_state.press_history.append((press_time, duration))

            # Cancel hold task
            if self._button_hold_task:
                self._button_hold_task.cancel()
                try:
                    await self._button_hold_task
                except asyncio.CancelledError:
                    pass
                self._button_hold_task = None

            self._emit_button_event(ButtonEvent.RELEASED, self.button_state)
            if self.on_button:
                try:
                    self.on_button(False)
                except Exception as e:
                    logger.error(f"Error in button callback: {e}")

        self._last_button_raw_value = raw_value
        self.state.button_pressed = self.button_state.is_pressed

    async def _button_hold_loop(self) -> None:
        """Emit HOLD events while button is pressed."""
        try:
            await asyncio.sleep(0.3)  # Initial delay before first HOLD
            while self.button_state.is_pressed:
                self._emit_button_event(ButtonEvent.HOLD, self.button_state)
                await asyncio.sleep(self.button_hold_interval_ms / 1000.0)
        except asyncio.CancelledError:
            pass

    def _emit_button_event(self, event: ButtonEvent, state: ButtonState) -> None:
        """Call the user's button event callback."""
        if self.on_button_event:
            try:
                self.on_button_event(event, state)
            except Exception as e:
                logger.error(f"Error in button event callback: {e}")

    # ==================== Public: Button helpers ====================
    def is_button_pressed(self) -> bool:
        """Check if the button is currently pressed."""
        return self.button_state.is_pressed

    def get_press_duration(self) -> float:
        """How long the button has been held (seconds)."""
        return self.button_state.press_duration

    def was_double_press(self, window_ms: float = 500.0) -> bool:
        """
        Check if the last two presses were a double-press.

        Args:
            window_ms: Maximum milliseconds between presses

        Returns:
            True if a double-press was detected
        """
        return self.button_state.was_double_press(window_ms)

    async def wait_for_press(self, timeout: Optional[float] = None) -> bool:
        """
        Wait for a button press.

        Args:
            timeout: Maximum seconds to wait, or None for indefinite

        Returns:
            True if pressed, False if timed out
        """
        future = asyncio.Future()

        def handler(event: ButtonEvent, state: ButtonState) -> None:
            if event == ButtonEvent.PRESSED and not future.done():
                future.set_result(True)

        original_callback = self.on_button_event
        self.on_button_event = handler

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return False
        finally:
            self.on_button_event = original_callback

    # ==================== Internal: Notification handlers ====================
    def _handle_imu_quaternions(self, sender: BleakGATTCharacteristic, data: bytearray) -> None:
        """Handle quaternions IMU data."""
        if len(data) >= 8:
            q1, q2, q3, q4 = struct.unpack("<4h", data[:8])
            q = QuaternionData(q1=q1, q2=q2, q3=q3, q4=q4, raw_bytes=bytes(data))
            self.state.imu_quaternions = q
            self.state.last_update = time.time()
            self._history["imu_quaternions"].append((time.time(), q))
            if self.on_imu_quaternions:
                try:
                    self.on_imu_quaternions(q)
                except Exception as e:
                    logger.error(f"Error in IMU quaternions callback: {e}")

    def _handle_imu_raw(self, sender: BleakGATTCharacteristic, data: bytearray) -> None:
        """Handle raw IMU sensor data."""
        raw = RawData(raw_bytes=bytes(data))
        self.state.imu_raw = raw
        self._history["imu_raw"].append((time.time(), raw))
        if self.on_imu_raw:
            try:
                self.on_imu_raw(raw)
            except Exception as e:
                logger.error(f"Error in IMU raw callback: {e}")

    def _handle_imu_fused(self, sender: BleakGATTCharacteristic, data: bytearray) -> None:
        """Handle fused IMU sensor data."""
        acc = FusedData(raw_bytes=bytes(data))
        self.state.imu_fused = acc
        self._history["imu_fused"].append((time.time(), acc))
        if self.on_imu_fused:
            try:
                self.on_imu_fused(acc)
            except Exception as e:
                logger.error(f"Error in IMU data callback: {e}")

    def _handle_temperature(self, sender: BleakGATTCharacteristic, data: bytearray) -> None:
        """Handle temperature sensor data."""
        if len(data) >= 2:
            temp = struct.unpack("<h", data[:2])[0]
            self.state.temperature = temp
            self._history["temperature"].append((time.time(), temp))
            if self.on_temperature:
                try:
                    self.on_temperature(temp)
                except Exception as e:
                    logger.error(f"Error in temperature callback: {e}")

    def _handle_battery(self, sender: BleakGATTCharacteristic, data: bytearray) -> None:
        """Handle battery level notification."""
        self._parse_battery_data(data)

    def _parse_battery_data(self, data: bytes) -> None:
        """Parse battery level from raw bytes."""
        if len(data) >= 1:
            batt = data[0]
            self.state.battery = batt
            self._history["battery"].append((time.time(), batt))
            if self.on_battery:
                try:
                    self.on_battery(batt)
                except Exception as e:
                    logger.error(f"Error in battery callback: {e}")

    # ==================== Public: Device control ====================
    async def set_led(self, color: Color, pattern: LedPattern = LedPattern.FIXED) -> None:
        """
        Set the wand's RGB LED color.

        Args:
            color: A Color instance - either a predefined color like Color.TEAL
                   or a custom Color(r, g, b)
            enabled: Whether the light is on or off (default: True)

        Example:
            >>> await wand.set_led(Color.PURPLE)
            >>> await wand.set_led(Color(128, 64, 32))       # Custom orange-ish
            >>> await wand.set_led(Color.RED, pattern = LedPattern.PULSE)  # Pulse red
        """
        if not self._connected:
            raise RuntimeError("Not connected to wand")

        # Convert between RGB888 and Kano's format (RGB565)
        rgb = (color.r >> 3) << 11 | (color.g >> 2) << 5 | color.b >> 3

        # Send message: [pattern, high_byte, low_byte]
        await self._client.write_gatt_char(WandUUIDs.RGB_LED, struct.pack(">BH", pattern, rgb), response=True)
        logger.debug(f"LED set to RGB({color.r},{color.g},{color.b}) -> pattern={pattern}, raw=0x{rgb:04X}")

    async def vibrate(self, pattern: VibrationPattern = VibrationPattern.REGULAR) -> None:
        """
        Trigger vibration with the specified pattern.

        Args:
            pattern: One of the VibrationPattern enum values

        Example:
            >>> from wandpy import VibrationPattern
            >>> await wand.vibrate(VibrationPattern.BURST)
        """
        if not self._connected:
            raise RuntimeError("Not connected to wand")

        await self._client.write_gatt_char(WandUUIDs.VIBRATION, bytes([pattern]), response=True)
        logger.debug(f"Vibration pattern: {pattern.name}")

    async def reset_quaternions(self) -> None:
        """
        Reset the quaternion orientation reference.

        Call this when you want to zero the current orientation.
        """
        if not self._connected:
            raise RuntimeError("Not connected to wand")

        await self._client.write_gatt_char(WandUUIDs.RESET_QUATERNIONS, b"\x01", response=True)
        logger.info("Quaternions reset")

    async def calibrate_magnetometer(self) -> None:
        """
        Start magnetometer calibration.

        There's no official calibration instruction but I guess it's a 8-pattern.
        """
        if not self._connected:
            raise RuntimeError("Not connected to wand")

        await self._client.write_gatt_char(WandUUIDs.MAG_CALIBRATION, b"\x01", response=True)
        logger.info("Magnetometer calibration started")

    # ==================== Public: Data history ====================

    def get_history(self, data_type: str) -> deque:
        """
        Get historical data for a sensor type.

        Args:
            data_type: One of 'imu_quaternions', 'imu_raw', 'imu_fused', 'battery', 'temperature'

        Returns:
            Deque of (timestamp, value) tuples
        """
        return self._history.get(data_type, deque())

    def clear_history(self) -> None:
        """Clear all stored historical data."""
        for q in self._history.values():
            q.clear()
