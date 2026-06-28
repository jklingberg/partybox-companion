"""Tests for DeviceManager using MockTransport."""

from __future__ import annotations

import asyncio

import pytest
from partybox.bluetooth.mock import MockTransport
from partybox.device.partybox import PartyBoxDevice
from partybox.protocol.codec import encode
from partybox.protocol.constants import BATTERY_LEVEL_CHAR_UUID, BATTERY_SERVICE_UUID
from partybox.protocol.messages import FirmwareVersionRequest
from partyboxd.config import SpeakerSettings
from partyboxd.device.events import ConnectedEvent, DisconnectedEvent, PowerChangedEvent
from partyboxd.device.manager import DeviceManager, DeviceNotConnectedError

FIRMWARE_REQUEST = encode(FirmwareVersionRequest())
# Real hardware capture: firmware 26.2.10
FIRMWARE_RESPONSE = bytes.fromhex("AA22041a020a00")


def _settings(**kw: object) -> SpeakerSettings:
    defaults = {"scan_timeout": 0.1, "reconnect_delay": 0.0}
    return SpeakerSettings(**{**defaults, **kw})  # type: ignore[arg-type]


def _make_manager(settings: SpeakerSettings | None = None) -> DeviceManager:
    return DeviceManager(settings or _settings())


# ---------------------------------------------------------------------------
# snapshot before run()
# ---------------------------------------------------------------------------


def test_initial_snapshot_is_disconnected() -> None:
    manager = _make_manager()
    snap = manager.snapshot
    assert not snap.connected
    assert snap.address is None
    assert snap.firmware is None
    assert snap.battery is None


# ---------------------------------------------------------------------------
# _refresh populates snapshot
# ---------------------------------------------------------------------------


async def test_refresh_populates_firmware() -> None:
    manager = _make_manager()
    transport = MockTransport(address="AA:BB:CC:DD:EE:FF")
    transport.stub(FIRMWARE_REQUEST, FIRMWARE_RESPONSE)
    await transport.connect()
    device = PartyBoxDevice._from_transport(transport)

    await manager._refresh(device)

    snap = manager.snapshot
    assert snap.connected
    assert snap.firmware == "26.2.10"
    assert snap.battery is None
    assert snap.address == "AA:BB:CC:DD:EE:FF"


async def test_refresh_populates_battery_when_present() -> None:
    manager = _make_manager()
    services = frozenset([BATTERY_SERVICE_UUID])
    transport = MockTransport(address="AA:BB:CC:DD:EE:FF", services=services)
    transport.stub(FIRMWARE_REQUEST, FIRMWARE_RESPONSE)
    transport.stub_read(BATTERY_LEVEL_CHAR_UUID, bytes([84]))
    await transport.connect()
    device = PartyBoxDevice._from_transport(transport)

    await manager._refresh(device)

    assert manager.snapshot.battery == 84


async def test_refresh_tolerates_firmware_error() -> None:
    """If firmware query fails, snapshot still records connected=True with firmware=None."""
    manager = _make_manager(SpeakerSettings(scan_timeout=0.1, reconnect_delay=0.0))
    transport = MockTransport(address="AA:BB:CC:DD:EE:FF")
    await transport.connect()
    device = PartyBoxDevice._from_transport(transport)

    # Feed non-firmware notifications then drop so firmware_version() raises quickly.
    transport.feed(b"\xaa\x00\x00")
    transport.feed(b"\xaa\x00\x00")
    transport.drop()

    await manager._refresh(device)

    snap = manager.snapshot
    assert snap.firmware is None


# ---------------------------------------------------------------------------
# snapshot clears on disconnect
# ---------------------------------------------------------------------------


async def test_snapshot_clears_after_connection_lost() -> None:
    manager = _make_manager()
    transport = MockTransport(address="AA:BB:CC:DD:EE:FF")
    transport.stub(FIRMWARE_REQUEST, FIRMWARE_RESPONSE)
    await transport.connect()
    device = PartyBoxDevice._from_transport(transport)

    await manager._refresh(device)
    assert manager.snapshot.connected

    # Simulate what _connect_and_maintain does in the finally block.
    from partyboxd.device.manager import StatusSnapshot

    manager._device = None
    manager._snapshot = StatusSnapshot(connected=False, address=None, firmware=None, battery=None)
    assert not manager.snapshot.connected
    assert manager.snapshot.address is None


# ---------------------------------------------------------------------------
# run() lifecycle
# ---------------------------------------------------------------------------


async def test_run_cancels_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    """run() exits cleanly when cancelled before finding a speaker."""

    async def _no_speaker(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr("partyboxd.device.manager.Scanner.find", _no_speaker)

    manager = _make_manager()
    task = asyncio.create_task(manager.run())
    await asyncio.sleep(0)  # let it start
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_run_connects_and_snapshot_is_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """run() connects to the mock device and snapshot reflects live state."""
    transport = MockTransport(address="BB:CC:DD:EE:FF:AA")
    transport.stub(FIRMWARE_REQUEST, FIRMWARE_RESPONSE)

    connected_event = asyncio.Event()

    async def _fake_find(*_: object, **__: object) -> PartyBoxDevice:
        await transport.connect()
        device = PartyBoxDevice._from_transport(transport)
        connected_event.set()
        return device

    monkeypatch.setattr("partyboxd.device.manager.Scanner.find", _fake_find)

    manager = _make_manager()
    task = asyncio.create_task(manager.run())

    # Wait until the manager has connected and refreshed.
    await asyncio.wait_for(connected_event.wait(), timeout=2.0)
    await asyncio.sleep(0.05)  # let _refresh complete

    snap = manager.snapshot
    assert snap.connected
    assert snap.firmware == "26.2.10"
    assert snap.address == "BB:CC:DD:EE:FF:AA"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# power_on / power_off
# ---------------------------------------------------------------------------


async def test_power_on_raises_when_not_connected() -> None:
    manager = _make_manager()
    with pytest.raises(DeviceNotConnectedError):
        await manager.power_on()


async def test_power_off_raises_when_not_connected() -> None:
    manager = _make_manager()
    with pytest.raises(DeviceNotConnectedError):
        await manager.power_off()


async def test_power_on_sends_command_and_emits_event() -> None:
    manager = _make_manager()
    transport = MockTransport(address="AA:BB:CC:DD:EE:FF")
    await transport.connect()
    device = PartyBoxDevice._from_transport(transport)
    manager._device = device  # type: ignore[assignment]

    queue = manager.subscribe()
    await manager.power_on()

    event = queue.get_nowait()
    assert isinstance(event, PowerChangedEvent)
    assert event.state == "on"


async def test_power_off_sends_command_and_emits_event() -> None:
    manager = _make_manager()
    transport = MockTransport(address="AA:BB:CC:DD:EE:FF")
    await transport.connect()
    device = PartyBoxDevice._from_transport(transport)
    manager._device = device  # type: ignore[assignment]

    queue = manager.subscribe()
    await manager.power_off()

    event = queue.get_nowait()
    assert isinstance(event, PowerChangedEvent)
    assert event.state == "off"


# ---------------------------------------------------------------------------
# EventBus — subscribe / unsubscribe
# ---------------------------------------------------------------------------


async def test_connected_event_emitted_after_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """run() emits a ConnectedEvent once the speaker is connected and refreshed."""
    transport = MockTransport(address="BB:CC:DD:EE:FF:AA")
    transport.stub(FIRMWARE_REQUEST, FIRMWARE_RESPONSE)

    connected_event = asyncio.Event()

    async def _fake_find(*_: object, **__: object) -> PartyBoxDevice:
        await transport.connect()
        device = PartyBoxDevice._from_transport(transport)
        connected_event.set()
        return device

    monkeypatch.setattr("partyboxd.device.manager.Scanner.find", _fake_find)

    manager = _make_manager()
    queue = manager.subscribe()
    task = asyncio.create_task(manager.run())

    await asyncio.wait_for(connected_event.wait(), timeout=2.0)
    await asyncio.sleep(0.05)

    event = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert isinstance(event, ConnectedEvent)
    assert event.address == "BB:CC:DD:EE:FF:AA"
    assert event.firmware == "26.2.10"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_unsubscribe_stops_delivery() -> None:
    manager = _make_manager()
    queue = manager.subscribe()
    manager.unsubscribe(queue)
    manager._bus.emit(DisconnectedEvent())
    assert queue.empty()
