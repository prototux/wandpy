"""Fake BLE clients simulating a Kano and a Magic Caster wand."""

from __future__ import annotations

import asyncio
import struct
from typing import Callable, Dict, List, Tuple

import pytest

import wandpy.wand
from wandpy.drivers.kano import KanoUUIDs
from wandpy.drivers.magic_caster import MagicCasterUUIDs


class FakeServices:
    def __init__(self, uuids):
        self.uuids = {u.lower() for u in uuids}

    def get_service(self, uuid):
        return object() if uuid.lower() in self.uuids else None


class FakeClient:
    """Minimal stand-in for bleak.BleakClient."""

    service_uuids: Tuple[str, ...] = ()
    instances: List["FakeClient"] = []

    def __init__(self, address, disconnected_callback=None, timeout=None, **kwargs):
        self.address = address
        self.disconnected_callback = disconnected_callback
        self.is_connected = False
        self.services = FakeServices(self.service_uuids)
        self.mtu_size = 247
        self.handlers: Dict[str, Callable] = {}
        self.writes: List[Tuple[str, bytes]] = []
        self.reads: Dict[str, bytes] = {}
        FakeClient.instances.append(self)

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        if self.is_connected:
            self.is_connected = False
            if self.disconnected_callback:
                self.disconnected_callback(self)

    def drop(self):
        """Simulate the wand going out of range."""
        self.is_connected = False
        self.disconnected_callback(self)

    async def start_notify(self, uuid, handler):
        self.handlers[uuid.lower()] = handler

    async def stop_notify(self, uuid):
        self.handlers.pop(uuid.lower(), None)

    async def read_gatt_char(self, uuid):
        return bytearray(self.reads.get(uuid.lower(), b"\x00"))

    async def write_gatt_char(self, uuid, data, response=False):
        self.writes.append((uuid.lower(), bytes(data)))
        self.on_write(uuid.lower(), bytes(data))

    def on_write(self, uuid, data):
        pass

    def notify(self, uuid, data: bytes):
        self.handlers[uuid.lower()](None, bytearray(data))

    def written(self, uuid) -> List[bytes]:
        return [d for u, d in self.writes if u == uuid.lower()]


class FakeKanoClient(FakeClient):
    service_uuids = (KanoUUIDs.WAND_SOFTWARE_INFO, KanoUUIDs.WAND_DATA)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reads = {
            KanoUUIDs.BATTERY: b"\x55",
            KanoUUIDs.BUTTON: b"\x00",
            KanoUUIDs.MAKER_NAME: b"Kano",
            KanoUUIDs.HARDWARE_DESC: b"\x01\x02",
        }


class FakeMagicCasterClient(FakeClient):
    """Answers the Magic Caster requests like the real wand (captured values)."""

    service_uuids = (MagicCasterUUIDs.SERVICE,)

    ANSWERS = {
        b"\x00": b"\x00\x00\x03",
        b"\x01": b"\x01\x40\x01",
        b"\x09": bytes.fromhex("09d16d87b741e4"),
        b"\x0e\x01": bytes.fromhex("0e01e71d2d04"),
        b"\x0e\x02": b"\x0e\x02" + b"883929800179",
        b"\x0e\x04": b"\x0e\x04" + b"WBMC22G1SDFW",
        b"\xfb": b"\xfb",
        b"\xfc": b"\xfc",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reads = {MagicCasterUUIDs.BATTERY: b"\x4b"}
        self.thresholds = [5, 5, 5, 5, 8, 8, 8, 8]

    def on_write(self, uuid, data):
        if uuid != MagicCasterUUIDs.COMMAND:
            return
        answer = self.ANSWERS.get(data)
        if data[0] == 0xDD:
            answer = bytes([0xDD, data[1], self.thresholds[data[1]]])
        elif data[0] == 0xDC:
            self.thresholds[data[1]] = data[2]
        if answer is not None:
            asyncio.get_running_loop().call_soon(self.notify, MagicCasterUUIDs.NOTIFY, answer)

    def commands(self) -> List[bytes]:
        return self.written(MagicCasterUUIDs.COMMAND)


@pytest.fixture
def fake_ble(monkeypatch):
    """Patch BleakClient; returns a function selecting which wand answers."""
    FakeClient.instances = []
    selected = {"cls": FakeKanoClient}

    def factory(*args, **kwargs):
        return selected["cls"](*args, **kwargs)

    monkeypatch.setattr(wandpy.wand, "BleakClient", factory)

    def use(cls):
        selected["cls"] = cls

    return use


def imu_packet(start: int, samples) -> bytes:
    """Build a Magic Caster IMU packet from (gx, gy, gz, ax, ay, az) samples."""
    body = b"".join(struct.pack("<6h", *s) for s in samples)
    return bytes([0x2C]) + struct.pack("<H", start) + bytes([len(samples)]) + body
