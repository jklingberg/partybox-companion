"""Unit tests for VolumeCapability."""

from __future__ import annotations

import pytest
from partybox.bluetooth.mock import MockTransport
from partybox.bluetooth.transport import NotConnectedError
from partybox.device.capabilities.volume import VolumeCapability
from partybox.device.partybox import PartyBoxDevice

# ---------------------------------------------------------------------------
# VolumeCapability — direct tests
# ---------------------------------------------------------------------------


async def test_get_raises_not_implemented() -> None:
    cap = VolumeCapability()
    with pytest.raises(NotImplementedError):
        await cap.get()


async def test_set_midrange_raises_not_implemented() -> None:
    cap = VolumeCapability()
    with pytest.raises(NotImplementedError):
        await cap.set(50)


@pytest.mark.parametrize("percent", [0, 100])
async def test_set_boundary_values_raise_not_implemented(percent: int) -> None:
    cap = VolumeCapability()
    with pytest.raises(NotImplementedError):
        await cap.set(percent)


@pytest.mark.parametrize("percent", [-1, 101, -100, 200])
async def test_set_out_of_range_raises_value_error(percent: int) -> None:
    cap = VolumeCapability()
    with pytest.raises(ValueError):
        await cap.set(percent)


async def test_set_range_error_checked_before_not_implemented() -> None:
    """ValueError for range must be raised even though NotImplementedError would follow."""
    cap = VolumeCapability()
    with pytest.raises(ValueError):
        await cap.set(-1)


# ---------------------------------------------------------------------------
# VolumeCapability via PartyBoxDevice
# ---------------------------------------------------------------------------


async def test_volume_present_after_connect() -> None:
    transport = MockTransport()
    async with transport:
        device = PartyBoxDevice._from_transport(transport)
        assert isinstance(device.volume, VolumeCapability)


async def test_volume_before_connect_raises() -> None:
    device = PartyBoxDevice.__new__(PartyBoxDevice)
    device._candidate = None
    device._transport = None
    device._power = None
    device._device_info = None
    device._battery = None
    device._volume = None
    with pytest.raises(NotConnectedError):
        _ = device.volume


async def test_volume_cleared_after_disconnect() -> None:
    transport = MockTransport()
    await transport.connect()
    device = PartyBoxDevice._from_transport(transport)
    assert isinstance(device.volume, VolumeCapability)
    await device.disconnect()
    with pytest.raises(NotConnectedError):
        _ = device.volume
