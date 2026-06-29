"""Tests for DeviceManager volume methods."""

from __future__ import annotations

import pytest
from partybox.bluetooth.mock import MockTransport
from partybox.device.partybox import PartyBoxDevice
from partyboxd.config import SpeakerSettings
from partyboxd.device.manager import DeviceManager, DeviceNotConnectedError


def _settings() -> SpeakerSettings:
    return SpeakerSettings(scan_timeout=0.1, reconnect_delay=0.0)


def _make_manager(
    volume_fallback: object = None,
) -> DeviceManager:
    return DeviceManager(_settings(), volume_fallback=volume_fallback)  # type: ignore[arg-type]


async def _connected_device() -> tuple[DeviceManager, MockTransport]:
    transport = MockTransport(address="AA:BB:CC:DD:EE:FF")
    await transport.connect()
    device = PartyBoxDevice._from_transport(transport)
    manager = _make_manager()
    manager._device = device  # type: ignore[assignment]
    return manager, transport


# ---------------------------------------------------------------------------
# get_volume
# ---------------------------------------------------------------------------


async def test_get_volume_raises_when_not_connected() -> None:
    manager = _make_manager()
    with pytest.raises(DeviceNotConnectedError):
        await manager.get_volume()


async def test_get_volume_returns_none_when_not_implemented() -> None:
    """SDK raises NotImplementedError (opcode TBD) → manager returns None."""
    manager, _ = await _connected_device()
    # SDK always raises NotImplementedError for volume.get() at this stage.
    result = await manager.get_volume()
    assert result is None


async def test_get_volume_uses_fallback_when_ble_not_implemented() -> None:
    """When BLE is not available, the fallback callable is consulted."""
    fallback_value = 65

    def fallback() -> int | None:
        return fallback_value

    transport = MockTransport(address="AA:BB:CC:DD:EE:FF")
    await transport.connect()
    device = PartyBoxDevice._from_transport(transport)
    manager = _make_manager(volume_fallback=fallback)
    manager._device = device  # type: ignore[assignment]

    result = await manager.get_volume()
    assert result == 65


async def test_get_volume_fallback_returns_none_if_no_known_value() -> None:
    """Fallback returning None propagates as None (no error)."""

    def fallback() -> int | None:
        return None

    transport = MockTransport(address="AA:BB:CC:DD:EE:FF")
    await transport.connect()
    device = PartyBoxDevice._from_transport(transport)
    manager = _make_manager(volume_fallback=fallback)
    manager._device = device  # type: ignore[assignment]

    result = await manager.get_volume()
    assert result is None


# ---------------------------------------------------------------------------
# set_volume
# ---------------------------------------------------------------------------


async def test_set_volume_raises_when_not_connected() -> None:
    manager = _make_manager()
    with pytest.raises(DeviceNotConnectedError):
        await manager.set_volume(50)


async def test_set_volume_raises_not_implemented_from_sdk() -> None:
    """SDK raises NotImplementedError for volume.set() — manager re-raises it."""
    manager, _ = await _connected_device()
    with pytest.raises(NotImplementedError):
        await manager.set_volume(50)


async def test_set_volume_raises_value_error_for_out_of_range() -> None:
    """SDK raises ValueError for bad percent — manager re-raises it."""
    manager, _ = await _connected_device()
    with pytest.raises(ValueError):
        await manager.set_volume(-1)
