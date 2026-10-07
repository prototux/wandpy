"""Tests of the data types, encoders and orientation filter."""

from __future__ import annotations

import struct

import pytest

from wandpy import Color, LedGroup, Macro, QuaternionData, RawData, Spell, WandType
from wandpy.drivers import detect_from_advertisement
from wandpy.drivers.kano import KanoUUIDs
from wandpy.drivers.magic_caster import MagicCasterUUIDs
from wandpy.fusion import MAGIC_CASTER_TO_KANO_AXES, OrientationFilter


def test_color():
    assert Color.OFF == Color(0, 0, 0) and Color.OFF.is_off
    assert Color.from_hex("#FF8800") == Color(255, 136, 0) == Color.from_hex("f80")
    assert Color.RED.to_rgb565() == 0xF800
    with pytest.raises(ValueError):
        Color(256, 0, 0)


def test_macro_encoding():
    macro = Macro().buzz(150).led(Color.GOLD, transition_ms=200).delay(400).clear()
    assert macro.to_bytes() == bytes.fromhex("685096002200ffd700c80010900120")
    looped = Macro().repeat(3, Macro().buzz(80))
    assert looped.to_bytes() == bytes.fromhex("688150500080" "03")
    assert len(Macro().led(Color.RED, LedGroup.ALL)) == 4
    with pytest.raises(ValueError):
        Macro().delay(70000)


def test_spell_parsing():
    spell = Spell.parse(b"\x24\x00\x00\x05Lumos")
    assert spell.name == "Lumos" and spell.matches("LUMOS")
    assert Spell.parse(b"\x24\x00\x00\x00") is None
    assert Spell.parse(b"\x24\x00\x00\x03\x00\x00\x00") is None


def test_raw_data_layouts():
    kano = RawData(struct.pack("<9h", *range(1, 10)))
    assert kano.accel == {"x": 1, "y": 2, "z": 3} and kano.mag_xyz == (4, 5, 6)
    assert kano.gyro == {"p": 7, "r": 8, "y": 9} and kano.accel_g is None

    mcw = RawData(struct.pack("<6h", 1, 2, 3, 2048, 0, -2048), wand_type=WandType.MAGIC_CASTER)
    assert mcw.valid and mcw.gyro_xyz == (1, 2, 3) and mcw.accel_g == (1.0, 0.0, -1.0)
    assert mcw.mag is None and mcw.gyro_dps is not None

    assert not RawData(b"\x00" * 5).valid


def test_quaternion_from_floats():
    q = QuaternionData.from_xyzw(0.0, 0.0, 0.0, 1.0)
    assert (q.q1, q.q2, q.q3, q.q4) == (0, 0, 0, 1024)
    assert q.euler == {"yaw": 0.0, "pitch": 0.0, "roll": 0.0}


def test_detection_from_advertisement():
    assert detect_from_advertisement("Kano-Wand-12-34-56", []) == WandType.KANO
    assert detect_from_advertisement(None, [KanoUUIDs.WAND_SOFTWARE_INFO]) == WandType.KANO
    assert detect_from_advertisement("MCW-DB32", []) == WandType.MAGIC_CASTER
    assert detect_from_advertisement(None, [MagicCasterUUIDs.SERVICE]) == WandType.MAGIC_CASTER
    assert detect_from_advertisement("MCB-1234", []) is None  # The box, not a wand
    assert detect_from_advertisement("Speaker", []) is None


def _euler(f: OrientationFilter):
    return QuaternionData.from_xyzw(*f.quaternion).euler


def test_orientation_filter_yaw_rotation():
    f = OrientationFilter(sample_rate=100.0, axis_map=MAGIC_CASTER_TO_KANO_AXES, estimate_gyro_bias=False)
    # Flat, then turning about the vertical (sensor Z) at 1 rad/s for 1 s
    f.update((0.0, 0.0, 0.0), (0.0, 0.0, 1.0))
    for _ in range(100):
        f.update((0.0, 0.0, 1.0), (0.0, 0.0, 1.0))
    e = _euler(f)
    assert abs(abs(e["yaw"]) - 57.3) < 2 and abs(e["pitch"]) < 1 and abs(e["roll"]) < 1


def test_orientation_filter_tilt_and_bias():
    f = OrientationFilter(sample_rate=100.0, axis_map=MAGIC_CASTER_TO_KANO_AXES)
    # Resting with a constant gyro bias: it gets learned and the yaw stops drifting
    for _ in range(3000):
        f.update((0.0, 0.0, 0.05), (0.0, 0.0, 1.0))
    assert abs(f.gyro_bias[1] - 0.05) < 0.005
    yaw = _euler(f)["yaw"]
    for _ in range(100):
        f.update((0.0, 0.0, 0.05), (0.0, 0.0, 1.0))
    assert abs(_euler(f)["yaw"] - yaw) < 0.5

    # Re-aligns on gravity when the tip points up
    f.reset()
    f.update((0.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    assert _euler(f)["pitch"] > 85  # Positive pitch = tip up


def test_orientation_follows_gyro_pitch():
    f = OrientationFilter(sample_rate=100.0, axis_map=MAGIC_CASTER_TO_KANO_AXES, estimate_gyro_bias=False, kp=0.0)
    f.update((0.0, 0.0, 0.0), (0.0, 0.0, 1.0))
    # Raise the tip: rotation about the sensor X axis (right), 0.5 rad/s for 1 s
    for _ in range(100):
        f.update((0.5, 0.0, 0.0), (0.0, 0.0, 1.0))
    assert abs(_euler(f)["pitch"] - 28.6) < 1
