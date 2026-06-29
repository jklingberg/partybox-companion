"""Unit tests for VolumeCapability.

The BLE volume opcode is not yet confirmed from hardware captures, so both
get() and set() raise NotImplementedError. The only live validation is the
percent range check in set(), which must raise ValueError before the
NotImplementedError path is reached.
"""

from __future__ import annotations

import pytest
from partybox.bluetooth.mock import MockTransport
from partybox.bluetooth.transport import NotConnectedError
from partybox.device.capabilities.volume import VolumeCapability
from partybox.device.partybox import PartyBoxDevice

# ---------------------------------------------------------------------------
# VolumeCapability in isolation
# ---------------------------------------------------------------------------


async def test_get_raises_not_implemented() -> None:
    transport = MockTransport()
    async with transport:
        cap = VolumeCapability(transport)
        with pytest.raises(NotImplementedError):
            await cap.get()


async def test_set_raises_not_implemented_for_valid_percent() -> None:
    transport = MockTransport()
    async with transport:
        cap = VolumeCapability(transport)
        with pytest.raises(NotImplementedError):
            await cap.set(50)


async def test_set_raises_value_error_for_negative_percent() -> None:
    transport = MockTransport()
    async with transport:
        cap = VolumeCapability(transport)
        with pytest.raises(ValueError):
            await cap.set(-1)


async def test_set_raises_value_error_above_100() -> None:
    transport = MockTransport()
    async with transport:
        cap = VolumeCapability(transport)
        with pytest.raises(ValueError):
            await cap.set(101)


async def test_set_accepts_boundary_values_0_and_100() -> None:
    """Boundary values 0 and 100 pass validation (NotImplementedError raised after)."""
    transport = MockTransport()
    async with transport:
        cap = VolumeCapability(transport)
        with pytest.raises(NotImplementedError):
            await cap.set(0)
        with pytest.raises(NotImplementedError):
            await cap.set(100)


# ---------------------------------------------------------------------------
# VolumeCapability via PartyBoxDevice
# ---------------------------------------------------------------------------


async def test_speaker_has_volume_capability() -> None:
    transport = MockTransport()
    async with transport:
        device = PartyBoxDevice._from_transport(transport)
        assert isinstance(device.volume, VolumeCapability)


async def test_speaker_volume_get_raises_not_implemented() -> None:
    transport = MockTransport()
    async with transport:
        device = PartyBoxDevice._from_transport(transport)
        with pytest.raises(NotImplementedError):
            await device.volume.get()


async def test_speaker_volume_set_raises_not_implemented() -> None:
    transport = MockTransport()
    async with transport:
        device = PartyBoxDevice._from_transport(transport)
        with pytest.raises(NotImplementedError):
            await device.volume.set(42)


async def test_speaker_volume_before_connect_raises() -> None:
    device = PartyBoxDevice.__new__(PartyBoxDevice)
    device._candidate = None
    device._transport = None
    device._power = None
    device._device_info = None
    device._battery = None
    device._volume = None
    with pytest.raises(NotConnectedError):
        _ = device.volume
