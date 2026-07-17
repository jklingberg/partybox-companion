"""Tests for DeviceManager using MockTransport."""

from __future__ import annotations

import asyncio

import pytest
from partybox import ConnectionFailedError
from partybox.bluetooth.mock import MockTransport
from partybox.device.partybox import PartyBoxDevice
from partybox.protocol.codec import encode
from partybox.protocol.messages import BatteryStatusRequest, FirmwareVersionRequest
from partyboxd.config import SpeakerSettings
from partyboxd.device.events import ConnectedEvent, DisconnectedEvent, PowerChangedEvent
from partyboxd.device.manager import (
    _RECONNECT_MAX,
    _WEDGE_WINDOW,
    DeviceManager,
    DeviceNotConnectedError,
)

FIRMWARE_REQUEST = encode(FirmwareVersionRequest())
# Real hardware capture: firmware 26.2.10
FIRMWARE_RESPONSE = bytes.fromhex("AA22041a020a00")

BATTERY_REQUEST = encode(BatteryStatusRequest())
# Real PartyBox 520 capture (on battery); derives to 90 %. See test_codec.py.
BATTERY_RESPONSE = bytes.fromhex(
    "aa9e3f01104850303030362d4350303034313234320202ca02030200000402c810"
    "0502b21206025c1207020100080163090102"
    "0a01000b04e00d00000c04cc060000"
)


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
    transport = MockTransport(address="AA:BB:CC:DD:EE:FF")
    transport.stub(FIRMWARE_REQUEST, FIRMWARE_RESPONSE)
    transport.stub(BATTERY_REQUEST, BATTERY_RESPONSE)
    await transport.connect()
    device = PartyBoxDevice._from_transport(transport, battery=True)

    await manager._refresh(device)

    assert manager.snapshot.battery == 90


async def test_poll_battery_recovers_missed_detection() -> None:
    """A battery missed at connect (speaker asleep) is recovered on a later poll."""
    manager = _make_manager()
    transport = MockTransport(address="AA:BB:CC:DD:EE:FF")
    transport.stub(FIRMWARE_REQUEST, FIRMWARE_RESPONSE)
    transport.stub(BATTERY_REQUEST, BATTERY_RESPONSE)
    await transport.connect()
    # Detection missed the battery at connect (battery=False), as in standby.
    device = PartyBoxDevice._from_transport(transport, battery=False)
    await manager._refresh(device)
    assert manager.snapshot.battery is None

    # Speaker is awake now; the periodic poll re-detects and fills in the level.
    await manager._poll_liveness(device)

    assert manager.snapshot.battery == 90
    assert manager.snapshot.battery_status is not None


async def test_poll_battery_clears_after_sustained_no_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A speaker that stops answering (standby) clears the cached reading, so
    a stale value is not served indefinitely — but only after a tolerance."""
    manager = _make_manager()
    transport = MockTransport(address="AA:BB:CC:DD:EE:FF")
    transport.stub(FIRMWARE_REQUEST, FIRMWARE_RESPONSE)
    transport.stub(BATTERY_REQUEST, BATTERY_RESPONSE)
    await transport.connect()
    device = PartyBoxDevice._from_transport(transport, battery=True)
    await manager._refresh(device)
    assert manager.snapshot.battery == 90

    # Speaker goes to standby: both firmware and battery now time out.
    async def _timeout(*_a: object, **_k: object) -> object:
        raise TimeoutError

    assert device.battery is not None
    assert device.device_info is not None
    monkeypatch.setattr(device.battery, "status", _timeout)
    monkeypatch.setattr(device.device_info, "firmware_version", _timeout)

    await manager._poll_liveness(device)  # first miss — tolerated, value kept
    assert manager.snapshot.battery == 90
    assert manager.snapshot.speaker_awake
    await manager._poll_liveness(device)  # second miss — cleared
    assert manager.snapshot.battery is None
    assert manager.snapshot.battery_status is None
    assert not manager.snapshot.speaker_awake


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


async def test_power_on_raises_when_not_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("partyboxd.device.manager._RECONNECT_WAIT_TIMEOUT", 0.05)
    manager = _make_manager()
    with pytest.raises(DeviceNotConnectedError):
        await manager.power_on()


async def test_power_off_raises_when_not_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("partyboxd.device.manager._RECONNECT_WAIT_TIMEOUT", 0.05)
    manager = _make_manager()
    with pytest.raises(DeviceNotConnectedError):
        await manager.power_off()


async def test_power_on_waits_for_in_progress_reconnect() -> None:
    """A power command mid-reconnect (self._device is None) should wait for
    the manager's connect loop to land a new connection, not fail instantly —
    this is the fix for "turn off, then immediately turn back on" racing the
    BLE reconnect that follows every power command on real hardware."""
    manager = _make_manager()
    transport = MockTransport(address="AA:BB:CC:DD:EE:FF")
    await transport.connect()
    device = PartyBoxDevice._from_transport(transport)

    async def _connect_shortly() -> None:
        await asyncio.sleep(0.02)
        manager._device = device  # type: ignore[assignment]
        manager._connected_event.set()

    connect_task = asyncio.create_task(_connect_shortly())
    queue = manager.subscribe()
    await manager.power_on()
    await connect_task

    event = queue.get_nowait()
    assert isinstance(event, PowerChangedEvent)
    assert event.state == "on"


async def test_power_on_raises_if_reconnect_does_not_land_in_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("partyboxd.device.manager._RECONNECT_WAIT_TIMEOUT", 0.05)
    manager = _make_manager()
    with pytest.raises(DeviceNotConnectedError):
        await manager.power_on()


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

    # _refresh() sets the initial snapshot (emitting SpeakerStateChangedEvent
    # for the off->on transition) before _connect_and_maintain emits
    # ConnectedEvent explicitly — find it regardless of exact ordering.
    event = await asyncio.wait_for(queue.get(), timeout=1.0)
    if not isinstance(event, ConnectedEvent):
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


# ---------------------------------------------------------------------------
# scan/connect retry backoff
# ---------------------------------------------------------------------------


async def test_scan_failure_backs_off_exponentially(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated scan failures double the retry delay up to _RECONNECT_MAX,
    instead of retrying at a flat `reconnect_delay` forever — the gap that let
    a wedged controller (ADR-028) get hammered with a scan roughly every 13s
    for the whole length of an outage."""

    async def _no_speaker(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr("partyboxd.device.manager.Scanner.find", _no_speaker)

    manager = _make_manager(_settings(reconnect_delay=1.0))

    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        if len(sleep_calls) >= 4:
            raise asyncio.CancelledError

    monkeypatch.setattr("partyboxd.device.manager.asyncio.sleep", fake_sleep)

    task = asyncio.create_task(manager.run())
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sleep_calls == [1.0, 2.0, 4.0, 8.0]


async def test_scan_backoff_caps_at_reconnect_max(monkeypatch: pytest.MonkeyPatch) -> None:
    """The backoff stops doubling once it reaches _RECONNECT_MAX."""

    async def _no_speaker(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr("partyboxd.device.manager.Scanner.find", _no_speaker)

    manager = _make_manager(_settings(reconnect_delay=_RECONNECT_MAX))

    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        if len(sleep_calls) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr("partyboxd.device.manager.asyncio.sleep", fake_sleep)

    task = asyncio.create_task(manager.run())
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sleep_calls == [_RECONNECT_MAX, _RECONNECT_MAX]


async def test_retry_delay_resets_after_successful_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful connect resets the backoff delay, not leaving a stale
    grown value in place from a prior run of failures."""
    transport = MockTransport(address="BB:CC:DD:EE:FF:AA")
    transport.stub(FIRMWARE_REQUEST, FIRMWARE_RESPONSE)

    async def _fake_find(*_: object, **__: object) -> PartyBoxDevice:
        await transport.connect()
        return PartyBoxDevice._from_transport(transport)

    monkeypatch.setattr("partyboxd.device.manager.Scanner.find", _fake_find)

    manager = _make_manager(_settings(reconnect_delay=1.0))
    manager._retry_delay = 32.0  # simulate prior backoff growth

    task = asyncio.create_task(manager.run())
    await asyncio.sleep(0.05)  # let it connect and refresh

    assert manager._retry_delay == 1.0

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# wedged-controller self-heal (ADR-039)
# ---------------------------------------------------------------------------


async def test_dense_connect_failures_trigger_adapter_recovery() -> None:
    """Three dense scan-found-it-but-connect-failed cycles invoke the injected
    recovery exactly once, then the counter starts over."""
    calls: list[bool] = []

    async def recover() -> bool:
        calls.append(True)
        return True

    manager = DeviceManager(_settings(), adapter_recover_fn=recover)
    await manager._note_connect_failure()
    await manager._note_connect_failure()
    assert calls == []
    await manager._note_connect_failure()
    assert calls == [True]
    # Counter was reset — a single further failure must not re-trigger.
    await manager._note_connect_failure()
    assert calls == [True]


async def test_stale_connect_failures_do_not_accumulate() -> None:
    """Failures further apart than _WEDGE_WINDOW restart the count, so
    isolated one-offs spread over days never add up to a recovery."""
    calls: list[bool] = []

    async def recover() -> bool:
        calls.append(True)
        return True

    manager = DeviceManager(_settings(), adapter_recover_fn=recover)
    await manager._note_connect_failure()
    await manager._note_connect_failure()
    # Age the run beyond the window; the next failure starts a fresh count.
    manager._last_connect_failure -= _WEDGE_WINDOW + 1
    await manager._note_connect_failure()
    await manager._note_connect_failure()
    assert calls == []
    await manager._note_connect_failure()
    assert calls == [True]


async def test_connect_failures_without_recover_fn_are_harmless() -> None:
    """Standalone partyboxd (no adapter_recover_fn) just keeps retrying."""
    manager = _make_manager()
    for _ in range(10):
        await manager._note_connect_failure()  # must not raise


async def test_connect_failure_loop_invokes_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end through run(): scan finds the speaker, connect keeps
    failing, recovery fires after the third cycle."""

    class _FailingDevice:
        async def connect(self) -> None:
            raise ConnectionFailedError("wedged")

    async def _find(*_: object, **__: object) -> _FailingDevice:
        return _FailingDevice()

    monkeypatch.setattr("partyboxd.device.manager.Scanner.find", _find)

    recovered = asyncio.Event()

    async def recover() -> bool:
        recovered.set()
        return True

    manager = DeviceManager(_settings(), adapter_recover_fn=recover)
    task = asyncio.create_task(manager.run())
    await asyncio.wait_for(recovered.wait(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
