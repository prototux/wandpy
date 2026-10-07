"""
Main Wand class for WandPy.

This is the primary interface for interacting with a BLE wand. The same class
drives the Kano Coding Wand and the Harry Potter Magic Caster Wand: the wand
type is detected when you connect, and every common feature (LED, vibration,
button, battery, orientation, raw IMU...) works the same way on both.

Basic usage:
    >>> from wandpy import Wand, Color, VibrationPattern
    >>> wand = Wand()
    >>> await wand.connect()                 # Nearest wand of any type
    >>> await wand.set_led(Color.TEAL)
    >>> await wand.vibrate(VibrationPattern.SHORT)

Callbacks for sensor data (plain or async functions):
    >>> def on_imu(data):
    ...     print(f"Yaw: {data.yaw:.1f}")
    >>> wand.on_imu_quaternions = on_imu

Wand-specific features are available on the same object; check them with
`wand.supports(Feature.SPELLS)` or catch UnsupportedFeatureError.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Callable, ClassVar, Deque, Dict, FrozenSet, List, Optional, Sequence, Tuple, Union

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from wandpy.button import ButtonEvent, ButtonState, ButtonTracker
from wandpy.constants import Color, Feature, LedGroup, LedPattern, VibrationPattern, WandType
from wandpy.device import DeviceInfo, WandInfo
from wandpy.drivers import WandDriver, detect_from_advertisement, detect_from_client, driver_for_type
from wandpy.errors import NotConnectedError, UnsupportedFeatureError
from wandpy.imu import FusedData, QuaternionData, RawData
from wandpy.macro import Macro
from wandpy.spell import Spell
from wandpy.utils import invoke, spawn

logger = logging.getLogger(__name__)

ConnectTarget = Union[str, WandInfo, BLEDevice, None]


class WandState:
    """
    Snapshot of the wand's current state.

    Attributes:
        connected: Whether the wand is currently connected
        wand_type: Type of the connected wand, or None before the first connection
        battery: Battery level percentage (0-100), or None if unknown
        temperature: Temperature in degrees (device units), or None (Kano only)
        button_pressed: Whether the button (Kano) or full grip (Magic Caster) is pressed
        buttons: Pressed state of each button/touch pad
        imu_quaternions: Latest quaternion data, or None
        imu_raw: Latest raw IMU sample, or None
        imu_fused: Latest filtered linear acceleration data, or None (Kano only)
        last_spell: Latest spell cast, or None (Magic Caster only)
        last_update: Timestamp of the most recent data update
    """

    def __init__(self):
        self.connected: bool = False
        self.wand_type: Optional[WandType] = None
        self.battery: Optional[int] = None
        self.temperature: Optional[float] = None
        self.button_pressed: bool = False
        self.buttons: Tuple[bool, ...] = ()
        self.imu_quaternions: Optional[QuaternionData] = None
        self.imu_raw: Optional[RawData] = None
        self.imu_fused: Optional[FusedData] = None
        self.last_spell: Optional[Spell] = None
        self.last_update: float = time.time()


class Wand:
    """
    Control a BLE wand: Kano Coding Wand or Harry Potter Magic Caster Wand.

    This class handles connection management, sensor data streaming,
    LED control, vibration, button input, and spells.

    Example:
        >>> async with Wand() as wand:              # Connects to the nearest wand
        ...     print(wand.wand_type, wand.info.model)
        ...     wand.on_button_event = lambda e, s: print(f"Button: {e.name}")
        ...     wand.on_spell = lambda spell: print(f"Cast {spell}")
        ...     await wand.set_led(Color.PURPLE)
        ...     await wand.vibrate()
    """

    #: Wand type used when none is given (set by KanoWand / MagicCasterWand)
    default_wand_type: ClassVar[Optional[WandType]] = None

    def __init__(
        self,
        mac_address: Optional[str] = None,
        wand_type: Optional[Union[WandType, str]] = None,
        *,
        keepalive_interval: Optional[float] = None,
        auto_reconnect: bool = True,
        imu_streaming: bool = True,
        button_thresholds: Optional[Sequence[Tuple[int, int]]] = None,
        button_debounce_ms: float = 20.0,
        button_hold_interval_ms: float = 50.0,
        button_poll_interval_ms: float = 15.0,
        button_poll_timeout_ms: float = 100.0,
        connect_timeout: float = 20.0,
    ):
        """
        Create a new Wand instance.

        Args:
            mac_address: Optional address to connect to. If not provided, pass
                         one to connect() or let connect() pick the nearest wand.
            wand_type: Force the wand type instead of detecting it.
            keepalive_interval: Seconds between keepalive packets (default:
                         what the wand needs - 3s on Kano, none on Magic Caster).
                         Use 0 to disable.
            auto_reconnect: Automatically reconnect on unexpected disconnect
            imu_streaming: Stream IMU data from the start (turn off to save
                         battery; use start_imu() later)
            button_thresholds: Magic Caster touch pad (min, max) sensitivity for
                         each of the 4 pads, applied on connect. None keeps the
                         wand's settings.
            button_debounce_ms: Minimum time between button state changes (default: 20)
            button_hold_interval_ms: Milliseconds between HOLD events (default: 50)
            button_poll_interval_ms: Kano button polling frequency in ms (default: 15)
            button_poll_timeout_ms: Kano: when to trust poll over notifications (default: 100)
            connect_timeout: Seconds to wait for the BLE connection
        """
        self.mac_address = mac_address
        self.wand_type: Optional[WandType] = WandType(wand_type) if wand_type else self.default_wand_type
        self.keepalive_interval = keepalive_interval
        self.auto_reconnect = auto_reconnect
        self.imu_streaming = imu_streaming
        self.button_thresholds = list(button_thresholds) if button_thresholds is not None else None
        self.connect_timeout = connect_timeout

        # BLE client and protocol driver
        self._client: Optional[BleakClient] = None
        self._driver: Optional[WandDriver] = None
        self._device: Optional[BLEDevice] = None
        self._connected = False
        self._closing = False
        self._keepalive_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self.info: Optional[DeviceInfo] = None

        # State tracking
        self.state = WandState()
        self.state.wand_type = self.wand_type
        self._history: Dict[str, Deque[Tuple[float, Any]]] = {
            "imu_quaternions": deque(maxlen=1000),
            "imu_raw": deque(maxlen=1000),
            "imu_fused": deque(maxlen=1000),
            "battery": deque(maxlen=100),
            "temperature": deque(maxlen=100),
            "buttons": deque(maxlen=100),
            "spells": deque(maxlen=100),
        }

        # Button configuration
        self.button_poll_interval_ms = button_poll_interval_ms
        self.button_poll_timeout_ms = button_poll_timeout_ms
        self._button = ButtonTracker(
            emit=self._emit_button_event,
            debounce_ms=button_debounce_ms,
            hold_interval_ms=button_hold_interval_ms,
        )
        self._press_waiters: List[asyncio.Future] = []
        self._spell_waiters: List[asyncio.Future] = []

        # --- User callbacks (plain functions or coroutines) ---
        # Called with (bool) when button is pressed/released (legacy)
        self.on_button: Optional[Callable[[bool], Any]] = None

        # Called with (ButtonEvent, ButtonState) for detailed button events
        self.on_button_event: Optional[Callable[[ButtonEvent, ButtonState], Any]] = None

        # Called with a tuple of booleans, one per button/touch pad, on every change
        self.on_buttons: Optional[Callable[[Tuple[bool, ...]], Any]] = None

        # IMU data callbacks - receive the data type objects
        self.on_imu_quaternions: Optional[Callable[[QuaternionData], Any]] = None
        self.on_imu_raw: Optional[Callable[[RawData], Any]] = None
        self.on_imu_fused: Optional[Callable[[FusedData], Any]] = None

        # Other sensor callbacks
        self.on_battery: Optional[Callable[[int], Any]] = None
        self.on_temperature: Optional[Callable[[float], Any]] = None

        # Called with a Spell when the wand recognizes one (Magic Caster)
        self.on_spell: Optional[Callable[[Spell], Any]] = None

        # Connection callbacks
        self.on_connect: Optional[Callable[[], Any]] = None
        self.on_disconnect: Optional[Callable[[], Any]] = None

    def __repr__(self) -> str:
        status = "connected" if self.is_connected else "disconnected"
        kind = self.wand_type.value if self.wand_type else "unknown"
        return f"<{type(self).__name__} {self.mac_address or '?'} {kind} {status}>"

    # ==================== Discovery ====================

    @classmethod
    async def scan(cls, timeout: float = 5.0, wand_type: Optional[Union[WandType, str]] = None) -> List[WandInfo]:
        """
        Scan for nearby wands.

        Works on the class or an instance: `await Wand.scan()`. KanoWand and
        MagicCasterWand only return wands of their type.

        Args:
            timeout: How long to scan in seconds
            wand_type: Only return wands of this type

        Returns:
            List of WandInfo (address, name, service_uuids, wand_type, rssi),
            strongest signal first
        """
        wanted = WandType(wand_type) if wand_type else cls.default_wand_type
        discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
        wands = []

        for device, advertisement in discovered.values():
            name = advertisement.local_name or device.name
            service_uuids = list(advertisement.service_uuids or [])
            detected = detect_from_advertisement(name, service_uuids)
            if detected is None or (wanted and detected != wanted):
                continue
            wands.append(
                WandInfo(
                    address=device.address,
                    name=name or "Unknown Wand",
                    service_uuids=service_uuids,
                    wand_type=detected,
                    rssi=advertisement.rssi,
                    device=device,
                )
            )

        wands.sort(key=lambda w: w.rssi if w.rssi is not None else -999, reverse=True)
        return wands

    # ==================== Connection ====================

    async def connect(self, target: ConnectTarget = None) -> bool:
        """
        Connect to a wand.

        Args:
            target: Address, WandInfo from scan(), or bleak BLEDevice. Uses the
                    address from __init__ if not provided; if there is none,
                    scans and connects to the nearest wand.

        Returns:
            True on successful connection, False otherwise
        """
        if self.is_connected:
            return True

        if target is None and self.mac_address is None:
            found = await self.scan(wand_type=self.wand_type)
            if not found:
                logger.error("No wand found")
                return False
            target = found[0]
            logger.info(f"Found {target}")

        if isinstance(target, WandInfo):
            self.mac_address = target.address
            self.wand_type = self.wand_type or target.wand_type
            self._device = target.device
        elif isinstance(target, BLEDevice):
            self.mac_address = target.address
            self._device = target
        elif target is not None:
            if target != self.mac_address:
                self._device = None
            self.mac_address = target

        self._closing = False
        client: Optional[BleakClient] = None
        try:
            # Create BLE client with disconnect callback
            client = BleakClient(
                self._device or self.mac_address,
                disconnected_callback=self._on_ble_disconnect,
                timeout=self.connect_timeout,
            )
            self._client = client
            await client.connect()
            await self._optimize_connection_params()

            if not client.is_connected:
                logger.error("Failed to connect")
                return False

            detected = detect_from_client(client)
            if detected is None and self.wand_type is None:
                logger.error(f"{self.mac_address} does not look like a supported wand")
                await client.disconnect()
                return False
            if detected is not None and self.wand_type is not None and detected != self.wand_type:
                logger.warning(f"Expected a {self.wand_type.value} wand, found a {detected.value} wand")
            self.wand_type = detected or self.wand_type
            self.state.wand_type = self.wand_type

            self._driver = driver_for_type(self.wand_type)(self, client)
            self._connected = True
            self.state.connected = True
            logger.info(f"Connected to {self.mac_address} ({self.wand_type.value})")

            # Set up notifications and wand-specific initialization
            await self._driver.start()

            # Start background tasks
            self._start_keepalive()

            # Read device info
            try:
                await self.get_device_info()
            except Exception as e:
                logger.warning(f"Error reading device info: {e}")

            invoke(self.on_connect, name="connect callback")
            return True

        except Exception as e:
            logger.error(f"Connection error: {e}")
            await self._teardown()
            if client is not None and client.is_connected:
                self._client = None  # So its disconnect callback is ignored
                try:
                    await client.disconnect()
                except Exception:
                    pass
            return False

    async def disconnect(self) -> None:
        """Disconnect from the wand and clean up all background tasks."""
        self._closing = True
        if self._reconnect_task and self._reconnect_task is not asyncio.current_task():
            self._reconnect_task.cancel()
        await self._teardown()

        if self._client and self._client.is_connected:
            await self._client.disconnect()
            logger.info("Disconnected")

    async def __aenter__(self) -> "Wand":
        if not await self.connect():
            raise ConnectionError("Could not connect to a wand")
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.disconnect()

    @property
    def is_connected(self) -> bool:
        """Whether the wand is currently connected."""
        return self._connected and self._client is not None and self._client.is_connected

    @property
    def address(self) -> Optional[str]:
        """Bluetooth address of the wand."""
        return self.mac_address

    @property
    def features(self) -> FrozenSet[Feature]:
        """Features of this wand (empty while the type is unknown)."""
        if self._driver is not None:
            return self._driver.features
        if self.wand_type is not None:
            return driver_for_type(self.wand_type).features
        return frozenset()

    def supports(self, feature: Feature) -> bool:
        """Whether this wand has `feature`."""
        return feature in self.features

    @property
    def button_count(self) -> int:
        """Number of buttons/touch pads reported by on_buttons (0 while unknown)."""
        if self.wand_type is None:
            return 0
        return driver_for_type(self.wand_type).button_count

    @property
    def driver(self) -> Optional[WandDriver]:
        """The protocol driver in use (advanced use)."""
        return self._driver

    async def get_device_info(self) -> DeviceInfo:
        """Read device information (model, firmware, serial...) from the wand."""
        driver = self._require()
        info = await driver.read_device_info()
        info.address = self.mac_address
        info.name = info.name or (self._device.name if self._device is not None else None)
        self.info = info
        return info

    # ==================== Internal: Connection management ====================

    async def _optimize_connection_params(self) -> None:
        """Try to negotiate low-latency BLE connection parameters."""
        try:
            if hasattr(self._client, "mtu_size"):
                logger.info(f"Negotiated MTU: {self._client.mtu_size}")

            # Platform-specific low-latency request (may not be available)
            if hasattr(self._client, "_request_connection_params"):
                # 7.5ms interval, 0 latency, 100ms timeout
                await self._client._request_connection_params(6, 6, 0, 100)
                logger.info("Requested low-latency connection parameters")
        except Exception as e:
            logger.debug(f"Could not optimize connection params: {e}")

    async def _teardown(self) -> None:
        """Stop background tasks and the driver."""
        self._connected = False
        self.state.connected = False

        tasks = [self._keepalive_task]
        self._keepalive_task = None
        for task in tasks:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        await self._button.close()
        if self._driver is not None:
            try:
                await self._driver.stop()
            except Exception as e:
                logger.debug(f"Error stopping driver: {e}")

    def _on_ble_disconnect(self, client: BleakClient) -> None:
        """Handle disconnection reported by bleak."""
        if client is not self._client or self._closing:
            return  # Superseded connection or disconnect() in progress

        logger.warning("Device disconnected unexpectedly")
        self._connected = False
        self.state.connected = False
        spawn(self._teardown())

        invoke(self.on_disconnect, name="disconnect callback")

        if self.auto_reconnect and (self._reconnect_task is None or self._reconnect_task.done()):
            self._reconnect_task = spawn(self._attempt_reconnect())

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

    def _start_keepalive(self) -> None:
        """Start the keepalive background task, if the wand needs one."""
        interval = self.keepalive_interval
        if interval is None:
            interval = self._driver.default_keepalive_interval
        if not interval or interval <= 0:
            return
        if self._keepalive_task:
            self._keepalive_task.cancel()
        self._keepalive_task = asyncio.create_task(self._keepalive_loop(interval))

    async def _keepalive_loop(self, interval: float) -> None:
        """Send periodic keepalive packets to maintain connection."""
        while self._connected:
            try:
                if self._client and self._client.is_connected:
                    await self._driver.keepalive()
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Keepalive error: {e}")
                await asyncio.sleep(1)

    def _require(self, feature: Optional[Feature] = None) -> WandDriver:
        """Return the driver, or raise if not connected / feature missing."""
        if not self._connected or self._driver is None:
            raise NotConnectedError()
        if feature is not None and feature not in self._driver.features:
            raise UnsupportedFeatureError(feature, self.wand_type)
        return self._driver

    # ==================== Internal: Data from drivers ====================

    def _record(self, key: str, value: Any) -> float:
        now = time.time()
        self.state.last_update = now
        self._history[key].append((now, value))
        return now

    def _emit_quaternions(self, data: QuaternionData) -> None:
        self.state.imu_quaternions = data
        self._record("imu_quaternions", data)
        invoke(self.on_imu_quaternions, data, name="IMU quaternions callback")

    def _emit_raw(self, data: RawData) -> None:
        self.state.imu_raw = data
        self._record("imu_raw", data)
        invoke(self.on_imu_raw, data, name="IMU raw callback")

    def _emit_fused(self, data: FusedData) -> None:
        self.state.imu_fused = data
        self._record("imu_fused", data)
        invoke(self.on_imu_fused, data, name="IMU fused callback")

    def _emit_temperature(self, value: float) -> None:
        self.state.temperature = value
        self._record("temperature", value)
        invoke(self.on_temperature, value, name="temperature callback")

    def _emit_battery(self, value: int) -> None:
        self.state.battery = value
        self._record("battery", value)
        invoke(self.on_battery, value, name="battery callback")

    def _emit_spell(self, spell: Spell) -> None:
        logger.info(f"Spell cast: {spell.name}")
        self.state.last_spell = spell
        self._record("spells", spell)
        for future in self._spell_waiters:
            if not future.done():
                future.set_result(spell)
        invoke(self.on_spell, spell, name="spell callback")

    def _emit_buttons(self, buttons: Tuple[bool, ...]) -> None:
        self.state.buttons = buttons
        self._record("buttons", buttons)
        invoke(self.on_buttons, buttons, name="buttons callback")

    async def _process_button(self, raw_value: int, from_poll: bool = False) -> None:
        """Feed the button state machine. Call with `self._button.lock` held."""
        await self._button.process(raw_value, from_poll)
        self.state.button_pressed = self._button.state.is_pressed

    def _emit_button_event(self, event: ButtonEvent, state: ButtonState) -> None:
        self.state.button_pressed = state.is_pressed
        invoke(self.on_button_event, event, state, name="button event callback")

        if event in (ButtonEvent.PRESSED, ButtonEvent.RELEASED):
            pressed = event == ButtonEvent.PRESSED
            invoke(self.on_button, pressed, name="button callback")
            if self.button_count == 1:
                self._emit_buttons((pressed,))
            if pressed:
                for future in self._press_waiters:
                    if not future.done():
                        future.set_result(True)

    # ==================== Public: Button helpers ====================

    @property
    def button_state(self) -> ButtonState:
        """Detailed state of the button (Kano) or full grip (Magic Caster)."""
        return self._button.state

    def is_button_pressed(self) -> bool:
        """Check if the button is currently pressed."""
        return self._button.state.is_pressed

    def get_press_duration(self) -> float:
        """How long the button has been held (seconds)."""
        return self._button.state.press_duration

    def was_double_press(self, window_ms: float = 500.0) -> bool:
        """
        Check if the last two presses were a double-press.

        Args:
            window_ms: Maximum milliseconds between presses

        Returns:
            True if a double-press was detected
        """
        return self._button.state.was_double_press(window_ms)

    async def wait_for_press(self, timeout: Optional[float] = None) -> bool:
        """
        Wait for a button press (Kano button or Magic Caster full grip).

        Args:
            timeout: Maximum seconds to wait, or None for indefinite

        Returns:
            True if pressed, False if timed out
        """
        future = asyncio.get_running_loop().create_future()
        self._press_waiters.append(future)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return False
        finally:
            self._press_waiters.remove(future)

    # ==================== Public: Spells ====================

    async def wait_for_spell(self, timeout: Optional[float] = None) -> Optional[Spell]:
        """
        Wait until a spell is cast.

        Args:
            timeout: Maximum seconds to wait, or None for indefinite

        Returns:
            The Spell, or None if timed out
        """
        self._require(Feature.SPELLS)
        future = asyncio.get_running_loop().create_future()
        self._spell_waiters.append(future)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._spell_waiters.remove(future)

    # ==================== Public: Light ====================

    async def set_led(
        self,
        color: Color,
        pattern: LedPattern = LedPattern.FIXED,
        *,
        group: Optional[LedGroup] = None,
        transition_ms: int = 0,
        duration_ms: Optional[int] = None,
    ) -> None:
        """
        Set the wand's LED color.

        Args:
            color: A Color instance - either a predefined color like Color.TEAL
                   or a custom Color(r, g, b). Color.OFF turns the light off.
            pattern: LED effect (native on Kano, emulated on Magic Caster)
            group: Magic Caster LED group (default: TIP; LedGroup.ALL for all).
                   Ignored on Kano, which has a single LED.
            transition_ms: Fade time to the new color (Magic Caster; immediate on Kano)
            duration_ms: Turn the light off after this many milliseconds

        Example:
            >>> await wand.set_led(Color.PURPLE)
            >>> await wand.set_led(Color(128, 64, 32))       # Custom orange-ish
            >>> await wand.set_led(Color.RED, LedPattern.PULSE_FAST)
            >>> await wand.set_led(Color.BLUE, group=LedGroup.ALL, transition_ms=500)
            >>> await wand.set_led(Color.GREEN, duration_ms=1000)
        """
        driver = self._require(Feature.LED)
        await driver.set_led(color, LedPattern(pattern), group, int(transition_ms), duration_ms)
        logger.debug(f"LED {color.to_hex()} pattern={LedPattern(pattern).name} group={group}")

    async def led_off(self) -> None:
        """Turn every LED off."""
        await self._require(Feature.LED).clear_leds()

    # ==================== Public: Haptics ====================

    async def vibrate(
        self,
        pattern: VibrationPattern = VibrationPattern.REGULAR,
        *,
        duration_ms: Optional[int] = None,
    ) -> None:
        """
        Vibrate the wand.

        Args:
            pattern: One of the VibrationPattern enum values
            duration_ms: Vibrate for this long instead of a pattern (exact on
                         Magic Caster, closest pattern on Kano)

        Example:
            >>> from wandpy import VibrationPattern
            >>> await wand.vibrate(VibrationPattern.BURST)
            >>> await wand.vibrate(duration_ms=250)
        """
        driver = self._require(Feature.VIBRATION)
        if duration_ms is not None:
            await driver.vibrate_for(int(duration_ms))
        else:
            await driver.vibrate(VibrationPattern(pattern))

    # ==================== Public: Macros (Magic Caster) ====================

    async def play_macro(self, macro: Macro, replace: bool = False) -> None:
        """
        Play a light/haptic sequence on the wand (Magic Caster).

        Args:
            macro: The Macro to play
            replace: Stop the currently playing macro first
        """
        await self._require(Feature.MACROS).play_macro(macro, replace)

    async def stop_macro(self) -> None:
        """Stop the macro currently playing (Magic Caster)."""
        await self._require(Feature.MACROS).stop_macro()

    # ==================== Public: IMU ====================

    async def start_imu(self) -> None:
        """Start streaming IMU data (quaternions and raw samples)."""
        await self._require(Feature.IMU_STREAM_CONTROL).start_imu()
        self.imu_streaming = True

    async def stop_imu(self) -> None:
        """Stop streaming IMU data to save battery and bandwidth."""
        await self._require(Feature.IMU_STREAM_CONTROL).stop_imu()
        self.imu_streaming = False

    async def reset_quaternions(self) -> None:
        """
        Reset the quaternion orientation reference.

        Call this when you want to zero the current orientation.
        """
        await self._require(Feature.ORIENTATION_RESET).reset_orientation()

    reset_orientation = reset_quaternions

    async def calibrate_imu(self) -> bool:
        """
        Calibrate the motion sensors. Keep the wand still on a flat surface.

        On Kano this starts the magnetometer calibration (move the wand in a
        figure 8). Returns True when the wand confirmed completion.
        """
        return await self._require(Feature.IMU_CALIBRATION).calibrate_imu()

    async def calibrate_magnetometer(self) -> None:
        """
        Start magnetometer calibration (Kano).

        There's no official calibration instruction but I guess it's a 8-pattern.
        """
        await self._require(Feature.MAGNETOMETER_CALIBRATION).calibrate_magnetometer()

    # ==================== Public: Touch pads (Magic Caster) ====================

    async def calibrate_buttons(self) -> bool:
        """Recalibrate the touch pad baseline. Don't touch the pads meanwhile."""
        return await self._require(Feature.BUTTON_CALIBRATION).calibrate_buttons()

    async def get_button_thresholds(self) -> List[Tuple[int, int]]:
        """Read the (min, max) sensitivity of each touch pad."""
        return await self._require(Feature.BUTTON_THRESHOLDS).get_button_thresholds()

    async def set_button_thresholds(
        self, thresholds: Union[Tuple[int, int], Sequence[Tuple[int, int]]]
    ) -> None:
        """
        Set the touch pad sensitivity.

        Args:
            thresholds: One (min, max) pair for every pad, or a list with one
                        pair per pad. The wand's defaults are (5, 8).
        """
        driver = self._require(Feature.BUTTON_THRESHOLDS)
        if len(thresholds) == 2 and all(isinstance(v, int) for v in thresholds):
            thresholds = [tuple(thresholds)] * driver.button_count
        await driver.set_button_thresholds(list(thresholds))

    # ==================== Public: Data history ====================

    def get_history(self, data_type: str) -> deque:
        """
        Get historical data for a sensor type.

        Args:
            data_type: One of 'imu_quaternions', 'imu_raw', 'imu_fused', 'battery',
                       'temperature', 'buttons', 'spells'

        Returns:
            Deque of (timestamp, value) tuples
        """
        return self._history.get(data_type, deque())

    def clear_history(self) -> None:
        """Clear all stored historical data."""
        for q in self._history.values():
            q.clear()


class KanoWand(Wand):
    """A Wand that only scans for and connects to Kano Coding Wands."""

    default_wand_type = WandType.KANO


class MagicCasterWand(Wand):
    """A Wand that only scans for and connects to Magic Caster Wands."""

    default_wand_type = WandType.MAGIC_CASTER


async def scan(timeout: float = 5.0, wand_type: Optional[Union[WandType, str]] = None) -> List[WandInfo]:
    """Scan for nearby wands of any (or the given) type. See Wand.scan()."""
    return await Wand.scan(timeout=timeout, wand_type=wand_type)


async def connect(
    target: ConnectTarget = None,
    wand_type: Optional[Union[WandType, str]] = None,
    **options: Any,
) -> Wand:
    """
    Connect to a wand and return it - the quickest way to get started.

        >>> wand = await wandpy.connect()            # Nearest wand
        >>> wand = await wandpy.connect("AA:BB:CC:DD:EE:FF")

    Args:
        target: Address, WandInfo or BLEDevice; None for the nearest wand
        wand_type: Only consider this type of wand
        **options: Any Wand() keyword argument

    Raises:
        ConnectionError: If no wand could be connected
    """
    wand = Wand(wand_type=wand_type, **options)
    if not await wand.connect(target):
        raise ConnectionError("Could not connect to a wand")
    return wand
