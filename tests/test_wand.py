"""End-to-end tests of the Wand API against simulated wands."""

from __future__ import annotations

import asyncio
import struct

import pytest

from conftest import FakeClient, FakeKanoClient, FakeMagicCasterClient, imu_packet
from wandpy import (
    ButtonEvent,
    Color,
    Feature,
    KanoWand,
    LedGroup,
    LedPattern,
    Macro,
    MagicCasterWand,
    NotConnectedError,
    UnsupportedFeatureError,
    VibrationPattern,
    Wand,
    WandType,
)
from wandpy.drivers.kano import KanoUUIDs
from wandpy.drivers.magic_caster import MagicCasterUUIDs

ADDRESS = "AA:BB:CC:DD:EE:FF"


def run(coro):
    return asyncio.run(coro)


async def settle(seconds: float = 0.05):
    await asyncio.sleep(seconds)


# ==================== Common behaviour ====================


@pytest.mark.parametrize(
    "client_cls, wand_type",
    [(FakeKanoClient, WandType.KANO), (FakeMagicCasterClient, WandType.MAGIC_CASTER)],
)
def test_same_code_works_on_both_wands(fake_ble, client_cls, wand_type):
    fake_ble(client_cls)

    async def scenario():
        events, buttons, quats, batteries = [], [], [], []
        wand = Wand(ADDRESS)
        wand.on_button_event = lambda e, s: events.append(e)
        wand.on_buttons = buttons.append
        wand.on_imu_quaternions = quats.append
        wand.on_battery = batteries.append

        assert await wand.connect()
        assert wand.wand_type == wand_type
        assert wand.supports(Feature.LED) and wand.supports(Feature.IMU_QUATERNIONS)
        assert wand.state.battery is not None and batteries

        await wand.set_led(Color.TEAL)
        await wand.set_led(Color.RED, LedPattern.BLINK)
        await wand.vibrate(VibrationPattern.BURST)
        await wand.vibrate(duration_ms=250)
        await wand.reset_quaternions()

        client = FakeClient.instances[-1]
        if wand_type == WandType.KANO:
            client.notify(KanoUUIDs.BUTTON, b"\x01")
            await settle()
            client.notify(KanoUUIDs.BUTTON, b"\x00")
            client.notify(KanoUUIDs.IMU_QUATERNIONS, struct.pack("<4h", 0, 0, 0, 1024))
        else:
            client.notify(MagicCasterUUIDs.NOTIFY, b"\x10\x0f")
            await settle()
            client.notify(MagicCasterUUIDs.NOTIFY, b"\x10\x00")
            client.notify(MagicCasterUUIDs.NOTIFY, imu_packet(0, [(0, 0, 0, 0, 0, 2048)] * 4))
        await settle()

        assert events[0] == ButtonEvent.PRESSED and events[-1] == ButtonEvent.RELEASED
        assert buttons[0][0] is True and buttons[-1][0] is False
        assert quats and abs(quats[-1].pitch) < 1 and abs(quats[-1].roll) < 1

        await wand.disconnect()
        assert not wand.is_connected

    run(scenario())


def test_commands_need_a_connection():
    async def scenario():
        with pytest.raises(NotConnectedError):
            await Wand().set_led(Color.RED)

    run(scenario())


def test_disconnect_does_not_reconnect(fake_ble):
    fake_ble(FakeKanoClient)

    async def scenario():
        wand = Wand(ADDRESS)
        disconnects = []
        wand.on_disconnect = lambda: disconnects.append(True)
        await wand.connect()
        await wand.disconnect()
        await settle()
        assert disconnects == [] and wand._reconnect_task is None

    run(scenario())


def test_unexpected_disconnect_reconnects(fake_ble, monkeypatch):
    fake_ble(FakeMagicCasterClient)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(min(s, 0.01)))

    async def scenario():
        wand = Wand(ADDRESS)
        disconnects = []
        wand.on_disconnect = lambda: disconnects.append(True)
        await wand.connect()
        FakeClient.instances[-1].drop()
        assert disconnects == [True] and not wand.is_connected
        await wand._reconnect_task
        assert wand.is_connected and len(FakeClient.instances) == 2
        await wand.disconnect()

    run(scenario())


def test_async_callbacks_are_awaited(fake_ble):
    fake_ble(FakeMagicCasterClient)

    async def scenario():
        wand = Wand(ADDRESS)
        seen = []

        async def on_spell(spell):
            await wand.set_led(Color.GOLD)
            seen.append(spell.name)

        wand.on_spell = on_spell
        await wand.connect()
        FakeClient.instances[-1].notify(MagicCasterUUIDs.NOTIFY, b"\x24\x00\x00\x05Lumos")
        await settle()
        assert seen == ["Lumos"]
        await wand.disconnect()

    run(scenario())


# ==================== Kano specifics ====================


def test_kano_protocol(fake_ble):
    fake_ble(FakeKanoClient)

    async def scenario():
        wand = KanoWand(ADDRESS)
        temps = []
        wand.on_temperature = temps.append
        await wand.connect()
        client = FakeClient.instances[-1]

        await wand.set_led(Color.RED, LedPattern.PULSE_SLOW)
        assert client.written(KanoUUIDs.RGB_LED)[-1] == struct.pack(">BH", 4, 0xF800)
        await wand.set_led(Color.OFF)
        assert client.written(KanoUUIDs.RGB_LED)[-1] == b"\x00\x00\x00"
        await wand.vibrate(VibrationPattern.LONG)
        assert client.written(KanoUUIDs.VIBRATION)[-1] == b"\x04"
        await wand.calibrate_magnetometer()
        assert client.written(KanoUUIDs.MAG_CALIBRATION) == [b"\x01"]

        client.notify(KanoUUIDs.IMU_TEMPERATURE, struct.pack("<h", 25))
        assert temps == [25]
        assert wand.info.manufacturer == "Kano"

        for call in (wand.wait_for_spell(), wand.play_macro(Macro().buzz(1)), wand.calibrate_buttons()):
            with pytest.raises(UnsupportedFeatureError):
                await call
        await wand.disconnect()

    run(scenario())


def test_kano_button_notifications_are_processed(fake_ble):
    """Notifications must not be starved by the poll loop."""
    fake_ble(FakeKanoClient)

    async def scenario():
        wand = Wand(ADDRESS, button_poll_interval_ms=10_000)  # Effectively no polling
        events = []
        wand.on_button_event = lambda e, s: events.append(e)
        await wand.connect()
        FakeClient.instances[-1].notify(KanoUUIDs.BUTTON, b"\x01")
        await settle()
        assert events == [ButtonEvent.PRESSED]
        await wand.disconnect()

    run(scenario())


def test_kano_led_duration(fake_ble):
    fake_ble(FakeKanoClient)

    async def scenario():
        wand = Wand(ADDRESS)
        await wand.connect()
        await wand.set_led(Color.BLUE, duration_ms=20)
        await settle()
        assert FakeClient.instances[-1].written(KanoUUIDs.RGB_LED)[-1] == b"\x00\x00\x00"
        await wand.disconnect()

    run(scenario())


# ==================== Magic Caster specifics ====================


def test_magic_caster_device_info(fake_ble):
    fake_ble(FakeMagicCasterClient)

    async def scenario():
        wand = Wand(ADDRESS)
        await wand.connect()
        info = wand.info
        assert info.wand_type == WandType.MAGIC_CASTER
        assert info.firmware_version == "0.3"
        assert info.model == "DEFIANT"
        assert info.device_id == "WBMC22G1SDFW"
        assert info.sku == "883929800179"
        assert info.serial_number == str(struct.unpack("<I", bytes.fromhex("e71d2d04"))[0])
        assert info.extra["box_address"] == "E4:41:B7:87:6D:D1"
        assert wand.state.battery == 0x4B
        # IMU streaming is started by default
        assert b"\x30\x00\x80" in FakeClient.instances[-1].commands()
        await wand.disconnect()
        assert FakeClient.instances[-1].commands()[-1] == b"\x31"

    run(scenario())


def test_magic_caster_led_and_haptics(fake_ble):
    fake_ble(FakeMagicCasterClient)

    async def scenario():
        wand = MagicCasterWand(ADDRESS, imu_streaming=False)
        await wand.connect()
        client = FakeClient.instances[-1]
        client.writes.clear()

        await wand.set_led(Color.RED)
        assert client.commands() == [b"\x60", b"\x42\x00\xff\x00\x00"]

        client.writes.clear()
        await wand.set_led(Color.BLUE, group=LedGroup.ALL)
        assert client.commands()[1:] == [bytes([0x42, g, 0, 0, 255]) for g in range(4)]

        client.writes.clear()
        await wand.set_led(Color.GREEN, group=LedGroup.POMMEL, transition_ms=300, duration_ms=1000)
        assert client.commands()[-1] == (
            Macro().led(Color.GREEN, LedGroup.POMMEL, 300).wait().delay(1000).led(Color.OFF, LedGroup.POMMEL).to_bytes()
        )

        client.writes.clear()
        await wand.led_off()
        assert client.commands() == [b"\x60", b"\x40"]

        client.writes.clear()
        await wand.vibrate(duration_ms=400)
        assert client.commands() == [b"\x68\x50\x90\x01"]

        client.writes.clear()
        await wand.play_macro(Macro().buzz(100).delay(50), replace=True)
        assert client.commands() == [b"\x60", b"\x68\x50\x64\x00\x10\x32\x00"]

        with pytest.raises(UnsupportedFeatureError):
            await wand.calibrate_magnetometer()
        await wand.disconnect()

    run(scenario())


def test_magic_caster_spells_and_pads(fake_ble):
    fake_ble(FakeMagicCasterClient)

    async def scenario():
        wand = Wand(ADDRESS)
        events, pads = [], []
        wand.on_button_event = lambda e, s: events.append(e)
        wand.on_buttons = pads.append
        await wand.connect()
        client = FakeClient.instances[-1]

        client.notify(MagicCasterUUIDs.NOTIFY, b"\x10\x01")  # Only the big pad
        await settle()
        assert pads[-1] == (True, False, False, False) and events == []
        client.notify(MagicCasterUUIDs.NOTIFY, b"\x10\x0f")  # Full grip
        await settle()
        assert events == [ButtonEvent.PRESSED] and wand.is_button_pressed()

        waiter = asyncio.ensure_future(wand.wait_for_spell(timeout=1))
        await settle(0)
        client.notify(MagicCasterUUIDs.NOTIFY, b"\x24\x00\x00\x10Expecto_Patronum")
        spell = await waiter
        assert spell.name == "Expecto Patronum" and spell.matches("expecto patronum")
        assert wand.state.last_spell is spell
        assert await wand.wait_for_spell(timeout=0.01) is None
        await wand.disconnect()

    run(scenario())


def test_magic_caster_imu(fake_ble):
    fake_ble(FakeMagicCasterClient)

    async def scenario():
        wand = Wand(ADDRESS)
        raws, quats = [], []
        wand.on_imu_raw = raws.append
        wand.on_imu_quaternions = quats.append
        await wand.connect()
        client = FakeClient.instances[-1]

        # Wand lying flat, then pointing up (shaft = sensor Y axis)
        client.notify(MagicCasterUUIDs.NOTIFY, imu_packet(0, [(16, -16, 0, 0, 0, 2048)] * 3))
        assert len(raws) == 3 and len(quats) == 1
        assert raws[0].accel_g == (0.0, 0.0, 1.0) and raws[0].sample_index == 0
        assert raws[0].gyro == {"x": 16, "y": -16, "z": 0}

        # Without gyro input only the (slow, drift-correcting) gravity term moves it
        for start in range(3, 3 + 3 * 1500, 3):
            client.notify(MagicCasterUUIDs.NOTIFY, imu_packet(start, [(0, 0, 0, 0, 2048, 0)] * 3))
        assert quats[-1].pitch > 85  # Positive pitch = tip up

        await wand.stop_imu()
        assert client.commands()[-1] == b"\x31"
        await wand.disconnect()

    run(scenario())


def test_magic_caster_calibration_and_thresholds(fake_ble):
    fake_ble(FakeMagicCasterClient)

    async def scenario():
        wand = Wand(ADDRESS, button_thresholds=[(7, 10)] * 4)
        await wand.connect()
        assert await wand.get_button_thresholds() == [(7, 10)] * 4
        await wand.set_button_thresholds((6, 9))
        assert await wand.get_button_thresholds() == [(6, 9)] * 4

        assert await wand.calibrate_imu() is True
        assert await wand.calibrate_buttons() is True
        commands = FakeClient.instances[-1].commands()
        assert commands[-2:] == [b"\xfe\x55\xaa", b"\xfb"]
        await wand.disconnect()

    run(scenario())
