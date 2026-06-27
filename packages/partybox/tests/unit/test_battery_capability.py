"""Unit tests for BatteryCapability using MockTransport."""

from partybox.bluetooth.mock import MockTransport
from partybox.device.capabilities.battery import BatteryCapability
from partybox.protocol.constants import BATTERY_LEVEL_CHAR_UUID


async def test_level_returns_percentage() -> None:
    transport = MockTransport()
    transport.stub_read(BATTERY_LEVEL_CHAR_UUID, bytes([75]))
    async with transport:
        cap = BatteryCapability(transport)
        assert await cap.level() == 75


async def test_level_zero() -> None:
    transport = MockTransport()
    transport.stub_read(BATTERY_LEVEL_CHAR_UUID, bytes([0]))
    async with transport:
        assert await BatteryCapability(transport).level() == 0


async def test_level_full() -> None:
    transport = MockTransport()
    transport.stub_read(BATTERY_LEVEL_CHAR_UUID, bytes([100]))
    async with transport:
        assert await BatteryCapability(transport).level() == 100
