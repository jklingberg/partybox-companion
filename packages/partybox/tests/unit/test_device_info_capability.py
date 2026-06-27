"""Unit tests for DeviceInfoCapability.

Firmware version (opcode 0x21 → 0x22) is confirmed from a JBL PartyBox 520.
Manufacturer is hardcoded "JBL" (no opcode required).
Model and serial number opcodes are not yet confirmed (NotImplementedError).

Byte fixtures from real hardware captures — see discoveries.md.
"""

import pytest
from partybox.bluetooth.mock import MockTransport
from partybox.device.capabilities.device_info import DeviceInfoCapability
from partybox.protocol.codec import encode
from partybox.protocol.messages import FirmwareVersionRequest

# Real hardware capture: AA 22 04 1a 02 0a 00 — PartyBox 520, firmware 26.2.10
FIRMWARE_REQUEST = encode(FirmwareVersionRequest())
FIRMWARE_RESPONSE = bytes.fromhex("AA22041a020a00")


async def test_manufacturer_returns_jbl() -> None:
    transport = MockTransport()
    async with transport:
        cap = DeviceInfoCapability(transport)
        assert await cap.manufacturer() == "JBL"


async def test_firmware_version() -> None:
    transport = MockTransport()
    transport.stub(FIRMWARE_REQUEST, FIRMWARE_RESPONSE)
    async with transport:
        cap = DeviceInfoCapability(transport)
        assert await cap.firmware_version() == "26.2.10"


async def test_firmware_version_ignores_unrelated_notifications() -> None:
    transport = MockTransport()
    async with transport:
        # Feed an unrelated notification first (power ACK), then the firmware response
        transport.feed(bytes.fromhex("AA00020300"))  # power ACK
        transport.feed(FIRMWARE_RESPONSE)
        cap = DeviceInfoCapability(transport)
        assert await cap.firmware_version() == "26.2.10"


async def test_model_raises_not_implemented() -> None:
    transport = MockTransport()
    async with transport:
        cap = DeviceInfoCapability(transport)
        with pytest.raises(NotImplementedError):
            await cap.model()


async def test_serial_number_raises_not_implemented() -> None:
    transport = MockTransport()
    async with transport:
        cap = DeviceInfoCapability(transport)
        with pytest.raises(NotImplementedError):
            await cap.serial_number()
