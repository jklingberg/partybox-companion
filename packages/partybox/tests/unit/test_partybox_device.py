"""Unit tests for PartyBoxDevice using MockTransport."""

import pytest
from partybox.bluetooth.mock import MockTransport
from partybox.bluetooth.transport import NotConnectedError
from partybox.device.capabilities.battery import BatteryCapability
from partybox.device.capabilities.device_info import DeviceInfoCapability
from partybox.device.capabilities.power import PowerCapability
from partybox.device.partybox import PartyBoxDevice
from partybox.protocol.constants import (
    BATTERY_LEVEL_CHAR_UUID,
    BATTERY_SERVICE_UUID,
)

POWER_ON_FRAME = bytes.fromhex("AA030105")


def _connected_transport(*, has_battery: bool = False) -> MockTransport:
    services = frozenset([BATTERY_SERVICE_UUID]) if has_battery else frozenset()
    t = MockTransport(services=services)
    if has_battery:
        t.stub_read(BATTERY_LEVEL_CHAR_UUID, bytes([80]))
    return t


async def test_capabilities_after_connect() -> None:
    transport = _connected_transport()
    async with transport:
        device = PartyBoxDevice._from_transport(transport)
        assert isinstance(device.power, PowerCapability)
        assert isinstance(device.device_info, DeviceInfoCapability)


async def test_battery_is_none_without_battery_service() -> None:
    transport = _connected_transport(has_battery=False)
    async with transport:
        device = PartyBoxDevice._from_transport(transport)
        assert device.battery is None


async def test_battery_present_with_battery_service() -> None:
    transport = _connected_transport(has_battery=True)
    async with transport:
        device = PartyBoxDevice._from_transport(transport)
        assert isinstance(device.battery, BatteryCapability)
        assert await device.battery.level() == 80


async def test_is_connected_reflects_transport_state() -> None:
    transport = _connected_transport()
    async with transport:
        device = PartyBoxDevice._from_transport(transport)
        assert device.is_connected


async def test_power_capability_works_through_device() -> None:
    transport = _connected_transport()
    async with transport:
        device = PartyBoxDevice._from_transport(transport)
        await device.power.turn_on()
    assert transport.writes == [POWER_ON_FRAME]


async def test_device_info_is_accessible_through_device() -> None:
    transport = _connected_transport()
    async with transport:
        device = PartyBoxDevice._from_transport(transport)
        assert isinstance(device.device_info, DeviceInfoCapability)


def _unconnected_device() -> PartyBoxDevice:
    """A PartyBoxDevice that has never been connected."""
    device = PartyBoxDevice.__new__(PartyBoxDevice)
    device._candidate = None
    device._transport = None
    device._power = None
    device._device_info = None
    device._battery = None
    return device


async def test_power_before_connect_raises() -> None:
    with pytest.raises(NotConnectedError):
        _ = _unconnected_device().power


async def test_device_info_before_connect_raises() -> None:
    with pytest.raises(NotConnectedError):
        _ = _unconnected_device().device_info


async def test_battery_before_connect_raises() -> None:
    with pytest.raises(NotConnectedError):
        _ = _unconnected_device().battery


async def test_capabilities_cleared_after_disconnect() -> None:
    transport = _connected_transport()
    await transport.connect()
    device = PartyBoxDevice._from_transport(transport)
    assert isinstance(device.power, PowerCapability)
    await device.disconnect()
    with pytest.raises(NotConnectedError):
        _ = device.power
    with pytest.raises(NotConnectedError):
        _ = device.device_info
    with pytest.raises(NotConnectedError):
        _ = device.battery


async def test_context_manager_disconnects_on_exit() -> None:
    transport = _connected_transport()
    await transport.connect()
    device = PartyBoxDevice._from_transport(transport)
    async with device:
        assert device.is_connected
    assert not transport.is_connected
