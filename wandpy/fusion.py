"""
Software orientation estimation for wands without on-device fusion.

The Kano wand computes its orientation itself and streams quaternions. The
Magic Caster wand only streams raw gyroscope/accelerometer samples, so WandPy
runs a Mahony filter (the same family of filter the official app uses) to
produce the same QuaternionData you get from a Kano wand.

Differences to keep in mind:
- There is no magnetometer on the Magic Caster, so yaw is relative to the
  orientation at connection time (or the last `reset_quaternions()`) and
  slowly drifts. Pitch and roll are absolute (gravity referenced).
- Gyroscope bias is estimated automatically whenever the wand rests still.

The output uses the frame of the Kano wand's quaternions (X right, Y up, tip
towards -Z, as in three.js which Kano's software is built on), so the Euler
angles of QuaternionData mean the same thing on both wands: positive pitch is
tip up. The Magic Caster axis mapping was inferred from community projects;
if your application needs another frame, pass your own `axis_map`.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence, Tuple

Vector = Tuple[float, float, float]

# (source axis index, sign) for each output axis.
# Magic Caster body frame: X right, Y towards the tip, Z up (when held for casting).
# Kano frame: X right, Y up, Z towards the hand (tip along -Z).
MAGIC_CASTER_TO_KANO_AXES: Tuple[Tuple[int, int], ...] = ((0, 1), (2, 1), (1, -1))
IDENTITY_AXES: Tuple[Tuple[int, int], ...] = ((0, 1), (1, 1), (2, 1))


def remap(v: Sequence[float], axis_map: Sequence[Tuple[int, int]]) -> Vector:
    """Reorder/flip a vector's axes according to `axis_map`."""
    return tuple(v[i] * s for i, s in axis_map)  # type: ignore[return-value]


class OrientationFilter:
    """
    Mahony attitude filter with world "up" along +Y.

    Example:
        >>> f = OrientationFilter(sample_rate=234.0)
        >>> x, y, z, w = f.update(gyro_rads=(0.0, 0.1, 0.0), accel=(0.0, 0.0, 1.0))
    """

    def __init__(
        self,
        sample_rate: float = 234.0,
        kp: float = 0.5,
        ki: float = 0.0,
        axis_map: Sequence[Tuple[int, int]] = IDENTITY_AXES,
        estimate_gyro_bias: bool = True,
        rest_gyro_threshold: float = 0.15,  # rad/s
        rest_accel_tolerance: float = 0.05,  # g
        rest_duration: float = 0.5,  # s
        bias_alpha: float = 0.02,
    ) -> None:
        self.sample_period = 1.0 / sample_rate
        self.kp = kp
        self.ki = ki
        self.axis_map = tuple(axis_map)
        self.estimate_gyro_bias = estimate_gyro_bias
        self.rest_gyro_threshold = rest_gyro_threshold
        self.rest_accel_tolerance = rest_accel_tolerance
        self.rest_samples_needed = max(1, int(rest_duration * sample_rate))
        self.bias_alpha = bias_alpha

        self.gyro_bias: Vector = (0.0, 0.0, 0.0)
        self.reset()

    def reset(self) -> None:
        """Forget the orientation; the next sample re-aligns pitch/roll and zeroes yaw."""
        self.w, self.x, self.y, self.z = 1.0, 0.0, 0.0, 0.0
        self._integral: Vector = (0.0, 0.0, 0.0)
        self._initialized = False
        self._rest_count = 0

    @property
    def quaternion(self) -> Tuple[float, float, float, float]:
        """Current orientation as (x, y, z, w), body to world."""
        return (self.x, self.y, self.z, self.w)

    def update(
        self, gyro_rads: Sequence[float], accel: Sequence[float], dt: Optional[float] = None
    ) -> Tuple[float, float, float, float]:
        """
        Feed one sample and return the new orientation as (x, y, z, w).

        Args:
            gyro_rads: Angular rate in rad/s, in the sensor frame.
            accel: Acceleration in g (any unit works for direction), sensor frame.
            dt: Seconds since the previous sample (defaults to 1/sample_rate).
        """
        dt = self.sample_period if dt is None else dt
        g = remap(gyro_rads, self.axis_map)
        a = remap(accel, self.axis_map)
        a_norm = math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])

        if self.estimate_gyro_bias:
            self._update_bias(g, a_norm)
        g = (g[0] - self.gyro_bias[0], g[1] - self.gyro_bias[1], g[2] - self.gyro_bias[2])

        if a_norm > 0.0:
            ax, ay, az = a[0] / a_norm, a[1] / a_norm, a[2] / a_norm

            if not self._initialized:
                self._align_to_gravity(ax, ay, az)
                self._initialized = True
                return self.quaternion

            # World up (+Y) expressed in the body frame: second row of R(q)
            w, x, y, z = self.w, self.x, self.y, self.z
            vx = 2.0 * (x * y + w * z)
            vy = 1.0 - 2.0 * (x * x + z * z)
            vz = 2.0 * (y * z - w * x)

            # Error between measured and estimated up direction
            ex = ay * vz - az * vy
            ey = az * vx - ax * vz
            ez = ax * vy - ay * vx

            if self.ki > 0.0:
                ix, iy, iz = self._integral
                self._integral = (ix + self.ki * ex * dt, iy + self.ki * ey * dt, iz + self.ki * ez * dt)
            ix, iy, iz = self._integral
            g = (g[0] + self.kp * ex + ix, g[1] + self.kp * ey + iy, g[2] + self.kp * ez + iz)

        # Integrate q_dot = 0.5 * q (x) (0, g)
        gx, gy, gz = g[0] * 0.5 * dt, g[1] * 0.5 * dt, g[2] * 0.5 * dt
        w, x, y, z = self.w, self.x, self.y, self.z
        self.w = w - x * gx - y * gy - z * gz
        self.x = x + w * gx + y * gz - z * gy
        self.y = y + w * gy - x * gz + z * gx
        self.z = z + w * gz + x * gy - y * gx
        self._normalize()
        return self.quaternion

    def _update_bias(self, g: Vector, a_norm: float) -> None:
        still = (
            abs(a_norm - 1.0) < self.rest_accel_tolerance
            and max(abs(g[0] - self.gyro_bias[0]), abs(g[1] - self.gyro_bias[1]), abs(g[2] - self.gyro_bias[2]))
            < self.rest_gyro_threshold
        )
        self._rest_count = self._rest_count + 1 if still else 0
        if self._rest_count >= self.rest_samples_needed:
            k = self.bias_alpha
            b = self.gyro_bias
            self.gyro_bias = (b[0] + k * (g[0] - b[0]), b[1] + k * (g[1] - b[1]), b[2] + k * (g[2] - b[2]))

    def _align_to_gravity(self, ax: float, ay: float, az: float) -> None:
        """Set the shortest rotation taking the measured up vector onto world +Y."""
        # q = (1 + u.v, u x v) with u = a, v = (0, 1, 0)
        w = 1.0 + ay
        if w < 1e-6:  # Upside down: rotate 180 degrees about X
            self.w, self.x, self.y, self.z = 0.0, 1.0, 0.0, 0.0
            return
        self.w, self.x, self.y, self.z = w, -az, 0.0, ax
        self._normalize()

    def _normalize(self) -> None:
        n = math.sqrt(self.w * self.w + self.x * self.x + self.y * self.y + self.z * self.z)
        if n == 0.0:
            self.w, self.x, self.y, self.z = 1.0, 0.0, 0.0, 0.0
            return
        self.w, self.x, self.y, self.z = self.w / n, self.x / n, self.y / n, self.z / n
