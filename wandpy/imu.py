"""
IMU data types for WandPy.

This module defines the data structures returned by the wand's IMU sensors.
Each class parses data at initialization, making both raw and parsed/converted
values available as attributes.

The same classes are used for every wand type:
- QuaternionData comes from the Kano wand's on-device fusion, or from WandPy's
  orientation filter for the Magic Caster wand (which only streams raw data).
- RawData holds a single accelerometer/gyroscope(/magnetometer) sample.
- FusedData is specific to the Kano wand.

Usage:
    >>> # Quaternion data
    >>> def on_imu(data):
    ...     print(data.raw)         # {'q1': 500, 'q2': -200, ...}
    ...     print(data.euler)       # {'yaw': 45.0, 'pitch': 10.0, 'roll': 0.0}
    ...     print(data.yaw)         # 45.0
    ...     print(data.to_dict())   # full dict representation
    ...     print(data.to_string()) # human-readable string
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from wandpy.constants import WandType

# Magic Caster IMU scale factors (from the official app's IMUSample)
MAGIC_CASTER_ACCEL_SCALE = 1.0 / 2048.0  # LSB -> g
MAGIC_CASTER_GYRO_SCALE = 0.0010908308  # LSB -> rad/s
MAGIC_CASTER_IMU_RATE_HZ = 234.0  # Samples per second

@dataclass
class QuaternionData:
    """
    Quaternion orientation data from the wand's IMU.

    The wand sends quaternions as four sint16 values ranging from -1024 to +1024,
    representing the [x, y, z, w] quaternion components scaled by 1024.

    Attributes:
        raw: The original integer values from the sensor as a dict.
        euler: Converted Euler angles (yaw, pitch, roll) in degrees.
        yaw: Compass heading in degrees (0° = north, 90° = east).
        pitch: Up/down tilt in degrees (positive = nose up).
        roll: Clockwise/anticlockwise rotation around forward axis in degrees.
        raw_bytes: Original bytes for advanced use/debugging.

    Example:
        >>> def on_quat(data):
        ...     print(f"Heading: {data.yaw:.1f}°")
    """

    # Raw quaternion components as received from the wand (-1024 to +1024)
    q1: int  # x component
    q2: int  # y component
    q3: int  # z component
    q4: int  # w component

    # Original bytes for advanced use/debugging
    raw_bytes: bytes = field(repr=False)

    # --- Parsed attributes (computed in __post_init__) ---
    raw: Dict[str, int] = field(init=False, repr=False)
    euler: Optional[Dict[str, float]] = field(init=False)
    yaw: Optional[float] = field(init=False)
    pitch: Optional[float] = field(init=False)
    roll: Optional[float] = field(init=False)

    def __post_init__(self) -> None:
        self.raw = {
            "q1": self.q1,
            "q2": self.q2,
            "q3": self.q3,
            "q4": self.q4,
        }
        self.euler = self._compute_euler()
        if self.euler is not None:
            self.yaw = self.euler["yaw"]
            self.pitch = self.euler["pitch"]
            self.roll = self.euler["roll"]
        else:
            self.yaw = None
            self.pitch = None
            self.roll = None

    @classmethod
    def from_xyzw(cls, x: float, y: float, z: float, w: float) -> "QuaternionData":
        """
        Build QuaternionData from a unit quaternion given as floats.

        Components are scaled by 1024 to match the Kano wire format, so data
        computed by WandPy behaves exactly like data sent by a Kano wand.
        """
        q = [max(-32768, min(32767, round(v * 1024.0))) for v in (x, y, z, w)]
        return cls(q1=q[0], q2=q[1], q3=q[2], q4=q[3], raw_bytes=struct.pack("<4h", *q))

    @property
    def xyzw(self) -> Tuple[float, float, float, float]:
        """The quaternion as normalized-scale floats (x, y, z, w)."""
        return (self.q1 / 1024.0, self.q2 / 1024.0, self.q3 / 1024.0, self.q4 / 1024.0)

    def _compute_euler(self) -> Optional[Dict[str, float]]:
        """
        Convert quaternion to Euler angles (yaw, pitch, roll) in degrees.
        Python adaptation of Kano's (non-standard) convention.

        Returns:
            Dictionary with 'yaw', 'pitch', 'roll' keys in degrees,
            or None if the quaternion is invalid (zero norm).
        """
        try:
            x, y, z, w = (
                self.q1 / 1024.0, self.q2 / 1024.0,
                self.q3 / 1024.0, self.q4 / 1024.0
            )

            # Normalize
            length = math.hypot(x, y, z, w)
            if length == 0.0:
                return None
            inv = 1.0 / length
            x, y, z, w = x * inv, y * inv, z * inv, w * inv

            # Full rotation matrix (same as JS makeRotationFromQuaternion)
            m00 = 1.0 - 2.0 * (y * y + z * z)
            m01 = 2.0 * (x * y - w * z)
            m02 = 2.0 * (x * z + w * y)
            m10 = 2.0 * (x * y + w * z)
            m11 = 1.0 - 2.0 * (x * x + z * z)
            m12 = 2.0 * (y * z - w * x)
            m20 = 2.0 * (x * z - w * y)
            m21 = 2.0 * (y * z + w * x)
            m22 = 1.0 - 2.0 * (x * x + y * y)

            # YXZ extraction: yaw about Y, pitch about X, roll about Z
            # This matches the Kano wand's natural orientation (Z-forward, Y-up).

            sin_pitch = -m12
            pitch_rad = math.asin(max(-1.0, min(1.0, sin_pitch)))
            cos_pitch = math.cos(pitch_rad)

            if abs(cos_pitch) < 0.0001:
                # Gimbal lock: pitch = ±90° (wand pointing straight up/down)
                pitch = math.copysign(90.0, sin_pitch)
                yaw = math.degrees(math.atan2(-m20, m21))
                roll = 0.0
            else:
                pitch = math.degrees(pitch_rad)
                # Yaw from m02/m22 (both share cos(pitch) factor)
                yaw = math.degrees(math.atan2(m02, m22))
                # Roll from m10/m11 (both share cos(pitch) factor)
                roll = math.degrees(math.atan2(m10, m11))

            def norm_angle(a):
                while a <= -180.0:
                    a += 360.0
                while a > 180.0:
                    a -= 360.0
                return round(a, 1)

            # Note: the JS convention negates roll. If you need exact JS parity,
            # change roll to -roll below. Otherwise this gives the standard sign.
            return {
                "yaw": norm_angle(yaw),
                "pitch": norm_angle(pitch),
                "roll": norm_angle(roll)
            }

        except Exception:
            return None

    def to_dict(self) -> Dict[str, Any]:
        """
        Return a dictionary representation of all parsed data.

        Returns:
            Dictionary with keys:
            - 'raw': dict of raw quaternion components
            - 'euler': dict of euler angles or None
            - 'yaw', 'pitch', 'roll': individual angle values or None
            - 'raw_bytes': hex string of original bytes
        """
        return {
            "raw": self.raw,
            "euler": self.euler,
            "yaw": self.yaw,
            "pitch": self.pitch,
            "roll": self.roll,
            "raw_bytes": self.raw_bytes.hex(),
        }

    def to_string(self) -> str:
        """
        Return a human-readable string representation.

        Returns:
            Formatted string with all parsed values.
        """
        if self.euler is not None:
            return (
                f"QuaternionData("
                f"raw={self.raw}, "
                f"yaw={self.yaw:.2f}°, "
                f"pitch={self.pitch:.2f}°, "
                f"roll={self.roll:.2f}°)"
            )
        return f"QuaternionData(raw={self.raw}, euler=None [invalid quaternion])"


@dataclass
class RawData:
    """
    Raw IMU sensor sample (accelerometer, gyroscope and, on Kano, magnetometer).

    Kano wand: 18 bytes containing 9 signed 16-bit values:
    - First 3 values: accelerometer (x, y, z)
    - Next 3 values: magnetometer (x, y, z)
    - Last 3 values: gyroscope (pitch, roll, yaw)

    Magic Caster wand: 12 bytes containing 6 signed 16-bit values:
    - First 3 values: gyroscope (x, y, z)
    - Last 3 values: accelerometer (x, y, z)
    The Magic Caster batches several samples per BLE packet; WandPy delivers
    them one at a time. Physical units are available in `accel_g` and
    `gyro_rads`/`gyro_dps`.

    Attributes:
        raw: The original integers as a tuple.
        accel: Accelerometer values as {'x', 'y', 'z'}.
        mag: Magnetometer values as {'x', 'y', 'z'} (None on Magic Caster).
        gyro: Gyroscope values as {'p', 'r', 'y'} on Kano, {'x', 'y', 'z'} on Magic Caster.
        accel_xyz, gyro_xyz, mag_xyz: The same values as tuples, in sensor
            order, for code that must work with any wand.
        accel_g: Acceleration in g, when the scale is known (Magic Caster).
        gyro_rads, gyro_dps: Angular rate in rad/s and deg/s, when the scale
            is known (Magic Caster).
        sample_index: Sample counter from the wand, when provided (Magic Caster).
        wand_type: Which wand produced the sample.
        raw_bytes: Original bytes for advanced use/debugging.
        valid: True if the data was parsed successfully.
        error: Error message if parsing failed, else None.
        raw_hex: Hex string of raw_bytes for debugging.

    Example:
        >>> def on_raw(data):
        ...     print(data.accel)
        ...     print(data.accel_xyz)
    """

    raw_bytes: bytes
    wand_type: WandType = WandType.KANO
    sample_index: Optional[int] = None

    # --- Parsed attributes (computed in __post_init__) ---
    raw: Optional[Tuple[int, ...]] = field(init=False)
    accel: Optional[Dict[str, int]] = field(init=False)
    mag: Optional[Dict[str, int]] = field(init=False)
    gyro: Optional[Dict[str, int]] = field(init=False)
    accel_xyz: Optional[Tuple[int, int, int]] = field(init=False, repr=False)
    gyro_xyz: Optional[Tuple[int, int, int]] = field(init=False, repr=False)
    mag_xyz: Optional[Tuple[int, int, int]] = field(init=False, repr=False)
    accel_g: Optional[Tuple[float, float, float]] = field(init=False, repr=False)
    gyro_rads: Optional[Tuple[float, float, float]] = field(init=False, repr=False)
    valid: bool = field(init=False)
    error: Optional[str] = field(init=False)
    raw_hex: str = field(init=False)

    def __post_init__(self) -> None:
        self.raw_hex = self.raw_bytes.hex()
        self.raw = None
        self.accel = None
        self.mag = None
        self.gyro = None
        self.accel_xyz = None
        self.gyro_xyz = None
        self.mag_xyz = None
        self.accel_g = None
        self.gyro_rads = None
        self.error = None

        expected = 12 if self.wand_type == WandType.MAGIC_CASTER else 18
        if len(self.raw_bytes) != expected:
            self.valid = False
            self.error = f"Expected {expected} bytes, got {len(self.raw_bytes)}"
            return

        try:
            if self.wand_type == WandType.MAGIC_CASTER:
                self._parse_magic_caster()
            else:
                self._parse_kano()
            self.valid = True
        except Exception as e:
            self.valid = False
            self.error = str(e)

    def _parse_kano(self) -> None:
        values = struct.unpack("<9h", self.raw_bytes)
        self.raw = values
        self.accel = {"x": values[0], "y": values[1], "z": values[2]}
        self.mag = {"x": values[3], "y": values[4], "z": values[5]}
        self.gyro = {"p": values[6], "r": values[7], "y": values[8]}
        self.accel_xyz = (values[0], values[1], values[2])
        self.mag_xyz = (values[3], values[4], values[5])
        self.gyro_xyz = (values[6], values[7], values[8])

    def _parse_magic_caster(self) -> None:
        values = struct.unpack("<6h", self.raw_bytes)
        self.raw = values
        self.gyro_xyz = (values[0], values[1], values[2])
        self.accel_xyz = (values[3], values[4], values[5])
        self.gyro = dict(zip("xyz", self.gyro_xyz))
        self.accel = dict(zip("xyz", self.accel_xyz))
        self.accel_g = tuple(v * MAGIC_CASTER_ACCEL_SCALE for v in self.accel_xyz)
        self.gyro_rads = tuple(v * MAGIC_CASTER_GYRO_SCALE for v in self.gyro_xyz)

    @property
    def gyro_dps(self) -> Optional[Tuple[float, float, float]]:
        """Angular rate in degrees per second, when the scale is known."""
        if self.gyro_rads is None:
            return None
        return tuple(math.degrees(v) for v in self.gyro_rads)

    def to_dict(self) -> Dict[str, Any]:
        """
        Return a dictionary representation of all parsed data.

        Returns:
            Dictionary with keys:
            - 'raw': tuple of values or None
            - 'accel', 'mag', 'gyro': sensor dicts or None
            - 'accel_g', 'gyro_rads': scaled tuples or None
            - 'sample_index': int or None
            - 'wand_type': str
            - 'valid': bool
            - 'error': error string or None
            - 'raw_hex': hex string of original bytes
        """
        return {
            "raw": self.raw,
            "accel": self.accel,
            "mag": self.mag,
            "gyro": self.gyro,
            "accel_g": self.accel_g,
            "gyro_rads": self.gyro_rads,
            "sample_index": self.sample_index,
            "wand_type": self.wand_type.value,
            "valid": self.valid,
            "error": self.error,
            "raw_hex": self.raw_hex,
        }

    def to_string(self) -> str:
        """
        Return a human-readable string representation.

        Returns:
            Formatted string with all parsed values.
        """
        if not self.valid:
            return f"RawData(valid=False, error='{self.error}', raw_hex={self.raw_hex})"
        ax, ay, az = self.accel_xyz
        gx, gy, gz = self.gyro_xyz
        mag = f"mag={self.mag_xyz}, " if self.mag_xyz is not None else ""
        return f"RawData(accel=({ax}, {ay}, {az}), {mag}gyro=({gx}, {gy}, {gz}))"


@dataclass
class FusedData:
    """
    Fused/filtered IMU output from the wand.

    This contains 18 bytes with 9 signed 16-bit values in a device-specific format.

    Attributes:
        raw: The original 9 integers as a tuple.
        filtered_accel: Filtered acceleration as {'x', 'y', 'z'}. (-32767 to +32767)
        pitch: Orientation-related pitch value. (-90 to +90)
        roll_sin: Trigonometric roll component (sine). (-1000 to +1000)
        roll_cos: Trigonometric roll component (cosine). (-1000 to +1000)
        compass: Heading value. (-1800 to +1800, overflows)
        absolute pitch: absolute pitch. (-1800 to +1800)
        roll: Orientation-related roll value. (-1800 to +1800, overflows)
        raw_bytes: Original bytes for advanced use/debugging.
        valid: True if the data was parsed successfully.
        error: Error message if parsing failed, else None.
        raw_hex: Hex string of raw_bytes for debugging.

    Example:
        >>> def on_fused(data):
        ...     print(data.filtered_accel)
        ...     print(data.compass)
    """

    raw_bytes: bytes

    # --- Parsed attributes (computed in __post_init__) ---
    raw: Optional[Tuple[int, ...]] = field(init=False)
    filtered_accel: Optional[Dict[str, int]] = field(init=False)
    pitch: Optional[int] = field(init=False)
    roll_sin: Optional[int] = field(init=False)
    roll_cos: Optional[int] = field(init=False)
    compass: Optional[int] = field(init=False)
    absolute_pitch: Optional[int] = field(init=False)
    roll: Optional[int] = field(init=False)
    valid: bool = field(init=False)
    error: Optional[str] = field(init=False)
    raw_hex: str = field(init=False)

    def __post_init__(self) -> None:
        self.raw_hex = self.raw_bytes.hex()
        self.raw = None
        self.filtered_accel = None
        self.pitch = None
        self.roll_sin = None
        self.roll_cos = None
        self.compass = None
        self.absolute_pitch = None
        self.roll = None
        self.error = None

        if len(self.raw_bytes) != 18:
            self.valid = False
            self.error = f"Expected 18 bytes, got {len(self.raw_bytes)}"
            return

        try:
            values = struct.unpack("<9h", self.raw_bytes)
            self.raw = values
            self.filtered_accel = {"x": values[0], "y": values[1], "z": values[2]}
            self.pitch = values[3]
            self.roll_sin = values[4]
            self.roll_cos = values[5]
            self.compass = values[6]
            self.absolute_pitch = values[7]
            self.roll = values[8]
            self.valid = True
        except Exception as e:
            self.valid = False
            self.error = str(e)

    def to_dict(self) -> Dict[str, Any]:
        """
        Return a dictionary representation of all parsed data.

        Returns:
            Dictionary with keys:
            - 'raw': tuple of 9 values or None
            - 'filtered_accel': dict or None
            - 'pitch', 'roll_sin', 'roll_cos', 'compass', 'absolute_pitch', 'roll': ints or None
            - 'valid': bool
            - 'error': error string or None
            - 'raw_hex': hex string of original bytes
        """
        return {
            "raw": self.raw,
            "filtered_accel": self.filtered_accel,
            "pitch": self.pitch,
            "roll_sin": self.roll_sin,
            "roll_cos": self.roll_cos,
            "compass": self.compass,
            "absolute_pitch": self.absolute_pitch,
            "roll": self.roll,
            "valid": self.valid,
            "error": self.error,
            "raw_hex": self.raw_hex,
        }

    def to_string(self) -> str:
        """
        Return a human-readable string representation.

        Returns:
            Formatted string with all parsed values.
        """
        if not self.valid:
            return (
                f"FusedData(valid=False, error='{self.error}', raw_hex={self.raw_hex})"
            )
        return (
            f"FusedData("
            f"filtered_accel=({self.filtered_accel['x']}, {self.filtered_accel['y']}, {self.filtered_accel['z']}), "
            f"pitch={self.pitch}, "
            f"roll_sin={self.roll_sin}, "
            f"roll_cos={self.roll_cos}, "
            f"compass={self.compass}, "
            f"absolute_pitch={self.absolute_pitch}, "
            f"roll={self.roll})"
        )
