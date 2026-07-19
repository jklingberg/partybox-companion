"""Tests for DeviceManager using MockTransport."""

from __future__ import annotations

import asyncio

import pytest
from partybox import ConnectionFailedError, ScanResult
from partybox.bluetooth.mock import MockTransport
from partybox.device.partybox import PartyBoxDevice
from partybox.protocol.codec import encode
from partybox.protocol.messages import BatteryStatusRequest, FirmwareVersionRequest
from partyboxd.config import SpeakerSettings
from partyboxd.device.events import (
    ConnectedEvent,
    DisconnectedEvent,
    PowerChangedEvent,
    SpeakerStateChangedEvent,
)
from partyboxd.device.manager import (
    _RECONNECT_MAX,
    _WEDGE_WINDOW,
    DeviceManager,
    DeviceNotConnectedError,
    StatusSnapshot,
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

    async def _no_speaker(*_: object, **__: object) -> ScanResult:
        return ScanResult(device=None, beacon_seen=False)

    monkeypatch.setattr("partyboxd.device.manager.Scanner.find_with_presence", _no_speaker)

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

    async def _fake_find(*_: object, **__: object) -> ScanResult:
        await transport.connect()
        device = PartyBoxDevice._from_transport(transport)
        connected_event.set()
        return ScanResult(device=device, beacon_seen=True)

    monkeypatch.setattr("partyboxd.device.manager.Scanner.find_with_presence", _fake_find)

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

    async def _fake_find(*_: object, **__: object) -> ScanResult:
        await transport.connect()
        device = PartyBoxDevice._from_transport(transport)
        connected_event.set()
        return ScanResult(device=device, beacon_seen=True)

    monkeypatch.setattr("partyboxd.device.manager.Scanner.find_with_presence", _fake_find)

    manager = _make_manager()
    queue = manager.subscribe()
    task = asyncio.create_task(manager.run())

    await asyncio.wait_for(connected_event.wait(), timeout=2.0)
    await asyncio.sleep(0.05)

    # _scan() (off->unreachable, once the beacon is seen) and _refresh()
    # (->on) each emit a SpeakerStateChangedEvent before
    # _connect_and_maintain emits ConnectedEvent explicitly — drain past
    # however many precede it rather than assuming an exact count.
    event = await asyncio.wait_for(queue.get(), timeout=1.0)
    while not isinstance(event, ConnectedEvent):
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

    async def _no_speaker(*_: object, **__: object) -> ScanResult:
        return ScanResult(device=None, beacon_seen=False)

    monkeypatch.setattr("partyboxd.device.manager.Scanner.find_with_presence", _no_speaker)

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

    async def _no_speaker(*_: object, **__: object) -> ScanResult:
        return ScanResult(device=None, beacon_seen=False)

    monkeypatch.setattr("partyboxd.device.manager.Scanner.find_with_presence", _no_speaker)

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

    async def _fake_find(*_: object, **__: object) -> ScanResult:
        await transport.connect()
        return ScanResult(device=PartyBoxDevice._from_transport(transport), beacon_seen=True)

    monkeypatch.setattr("partyboxd.device.manager.Scanner.find_with_presence", _fake_find)

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

    async def _find(*_: object, **__: object) -> ScanResult:
        return ScanResult(device=_FailingDevice(), beacon_seen=True)  # type: ignore[arg-type]

    monkeypatch.setattr("partyboxd.device.manager.Scanner.find_with_presence", _find)

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


async def test_maintain_exit_disconnects_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Leaving the maintain loop must release the old connection: a
    false-positive I/O timeout can exit the cycle while BlueZ still holds the
    speaker's single live connection, which would block every reconnect."""
    transport = MockTransport(address="BB:CC:DD:EE:FF:AA")
    transport.stub(FIRMWARE_REQUEST, FIRMWARE_RESPONSE)

    connected_event = asyncio.Event()
    disconnect_calls: list[bool] = []
    real_disconnect = transport.disconnect

    async def tracking_disconnect() -> None:
        disconnect_calls.append(True)
        await real_disconnect()

    monkeypatch.setattr(transport, "disconnect", tracking_disconnect)

    async def _fake_find(*_: object, **__: object) -> ScanResult:
        await transport.connect()
        device = PartyBoxDevice._from_transport(transport)
        connected_event.set()
        return ScanResult(device=device, beacon_seen=True)

    monkeypatch.setattr("partyboxd.device.manager.Scanner.find_with_presence", _fake_find)

    manager = _make_manager()
    task = asyncio.create_task(manager.run())
    await asyncio.wait_for(connected_event.wait(), timeout=2.0)
    await asyncio.sleep(0.05)  # let _refresh complete and drain start

    transport.drop()  # unexpected connection loss ends the maintain cycle
    for _ in range(50):
        if disconnect_calls:
            break
        await asyncio.sleep(0.01)
    assert disconnect_calls, "maintain-loop exit did not disconnect the transport"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_connect_failures_in_power_command_grace_are_not_counted() -> None:
    """ADR-034: the speaker resets its BLE stack after every power command;
    the connect failures that produces must not look like a wedge."""
    calls: list[bool] = []

    async def recover() -> bool:
        calls.append(True)
        return True

    manager = DeviceManager(_settings(), adapter_recover_fn=recover)
    manager._last_power_command = asyncio.get_running_loop().time()
    for _ in range(5):
        await manager._note_connect_failure()
    assert calls == []
    assert manager._connect_failures == 0


async def test_recovery_cooldown_suppresses_repeat_recovery() -> None:
    """A recovery that does not fix the failure mode must not repeat every
    few minutes (each one drops healthy A2DP audio); after the cool-down the
    next failure re-triggers immediately."""
    from partyboxd.device.manager import _RECOVERY_COOLDOWN

    calls: list[bool] = []

    async def recover() -> bool:
        calls.append(True)
        return True

    manager = DeviceManager(_settings(), adapter_recover_fn=recover)
    for _ in range(3):
        await manager._note_connect_failure()
    assert calls == [True]
    # Failure mode persists — further dense failures are suppressed.
    for _ in range(6):
        await manager._note_connect_failure()
    assert calls == [True]
    # Cool-down expires; the very next failure re-triggers.
    assert manager._last_recovery is not None
    manager._last_recovery -= _RECOVERY_COOLDOWN + 1
    await manager._note_connect_failure()
    assert calls == [True, True]


async def test_scan_errors_trigger_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scanner.find raising (adapter unusable — e.g. left powered off by a
    half-completed recovery) must also reach the recovery path, since no
    connect attempt will ever happen in that state."""
    from partyboxd.device.manager import _WEDGE_SCAN_ERRORS

    calls: list[bool] = []

    async def recover() -> bool:
        calls.append(True)
        return True

    async def _broken_scan(*_: object, **__: object) -> None:
        raise RuntimeError("org.bluez.Error.NotReady")

    monkeypatch.setattr("partyboxd.device.manager.Scanner.find_with_presence", _broken_scan)
    manager = DeviceManager(_settings(), adapter_recover_fn=recover)
    for _ in range(_WEDGE_SCAN_ERRORS):
        assert await manager._scan() is None
    assert calls == [True]


async def test_completed_scan_resets_scan_error_count(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _empty_scan(*_: object, **__: object) -> ScanResult:
        return ScanResult(device=None, beacon_seen=False)

    manager = _make_manager()
    manager._scan_errors = 4
    monkeypatch.setattr("partyboxd.device.manager.Scanner.find_with_presence", _empty_scan)
    assert await manager._scan() is None
    assert manager._scan_errors == 0


# ---------------------------------------------------------------------------
# stale-LE-connection reclaim (orphaned link from a dead process)
# ---------------------------------------------------------------------------


async def test_empty_scans_trigger_stale_reclaim(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three clean-but-empty scans invoke the injected reclaim exactly once,
    then the counter starts over."""
    calls: list[bool] = []

    async def reclaim() -> bool:
        calls.append(True)
        return False

    async def _empty_scan(*_: object, **__: object) -> ScanResult:
        return ScanResult(device=None, beacon_seen=False)

    monkeypatch.setattr("partyboxd.device.manager.Scanner.find_with_presence", _empty_scan)
    manager = DeviceManager(_settings(), stale_reclaim_fn=reclaim)
    await manager._scan()
    await manager._scan()
    assert calls == []
    await manager._scan()
    assert calls == [True]
    # Counter was reset — a single further empty scan must not re-trigger.
    await manager._scan()
    assert calls == [True]


async def test_successful_reclaim_resets_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reclaim that disconnected something resets the retry delay so the
    now-advertising speaker is found on the next cycle."""

    async def reclaim() -> bool:
        return True

    async def _empty_scan(*_: object, **__: object) -> ScanResult:
        return ScanResult(device=None, beacon_seen=False)

    monkeypatch.setattr("partyboxd.device.manager.Scanner.find_with_presence", _empty_scan)
    manager = DeviceManager(_settings(reconnect_delay=1.0), stale_reclaim_fn=reclaim)
    manager._retry_delay = 32.0  # simulate prior backoff growth
    for _ in range(3):
        await manager._scan()
    assert manager._retry_delay == 1.0


async def test_found_speaker_resets_empty_scan_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """A scan that finds the speaker restarts the empty-scan run, so ordinary
    off-periods interleaved with reconnects never accumulate to a reclaim."""
    calls: list[bool] = []

    async def reclaim() -> bool:
        calls.append(True)
        return False

    manager = DeviceManager(_settings(), stale_reclaim_fn=reclaim)
    results: list[object | None] = [None, None, object(), None, None]

    async def _scan_script(*_: object, **__: object) -> ScanResult:
        item = results.pop(0)
        return ScanResult(device=item, beacon_seen=item is not None)  # type: ignore[arg-type]

    monkeypatch.setattr("partyboxd.device.manager.Scanner.find_with_presence", _scan_script)
    for _ in range(5):
        await manager._scan()
    assert calls == []


async def test_reclaim_exception_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def reclaim() -> bool:
        raise RuntimeError("dbus exploded")

    async def _empty_scan(*_: object, **__: object) -> ScanResult:
        return ScanResult(device=None, beacon_seen=False)

    monkeypatch.setattr("partyboxd.device.manager.Scanner.find_with_presence", _empty_scan)
    manager = DeviceManager(_settings(), stale_reclaim_fn=reclaim)
    for _ in range(3):
        assert await manager._scan() is None  # must not raise


async def test_empty_scans_without_reclaim_fn_are_harmless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Standalone partyboxd (no stale_reclaim_fn) just keeps scanning."""

    async def _empty_scan(*_: object, **__: object) -> ScanResult:
        return ScanResult(device=None, beacon_seen=False)

    monkeypatch.setattr("partyboxd.device.manager.Scanner.find_with_presence", _empty_scan)
    manager = _make_manager()
    for _ in range(10):
        assert await manager._scan() is None  # must not raise


async def test_scan_error_does_not_count_as_empty_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Erroring scans say nothing about orphaned links; only clean empties count."""
    calls: list[bool] = []

    async def reclaim() -> bool:
        calls.append(True)
        return False

    async def _broken_scan(*_: object, **__: object) -> None:
        raise RuntimeError("org.bluez.Error.NotReady")

    monkeypatch.setattr("partyboxd.device.manager.Scanner.find_with_presence", _broken_scan)
    manager = DeviceManager(_settings(), stale_reclaim_fn=reclaim)
    for _ in range(4):
        await manager._scan()
    assert calls == []


async def test_connect_failure_with_stale_link_reclaims_instead_of_counting() -> None:
    """A connect failure while an orphaned LE link exists must reclaim the
    link and not count toward adapter recovery — the speaker refusing
    connects because its control slot is held is not a controller wedge."""
    recoveries: list[bool] = []
    reclaims: list[bool] = []

    async def recover() -> bool:
        recoveries.append(True)
        return True

    async def reclaim() -> bool:
        reclaims.append(True)
        return True

    manager = DeviceManager(
        _settings(reconnect_delay=1.0),
        adapter_recover_fn=recover,
        stale_reclaim_fn=reclaim,
    )
    manager._retry_delay = 32.0
    for _ in range(5):
        await manager._note_connect_failure()
    assert len(reclaims) == 5
    assert recoveries == []
    assert manager._connect_failures == 0
    assert manager._retry_delay == 1.0


async def test_connect_failures_still_count_when_nothing_to_reclaim() -> None:
    """With no stale link present (reclaim returns False), dense connect
    failures must still escalate to adapter recovery as before (ADR-039)."""
    recoveries: list[bool] = []

    async def recover() -> bool:
        recoveries.append(True)
        return True

    async def reclaim() -> bool:
        return False

    manager = DeviceManager(_settings(), adapter_recover_fn=recover, stale_reclaim_fn=reclaim)
    await manager._note_connect_failure()
    await manager._note_connect_failure()
    assert recoveries == []
    await manager._note_connect_failure()
    assert recoveries == [True]


# ---------------------------------------------------------------------------
# manually requested adapter reset (Portal "Reset Bluetooth" button)
# ---------------------------------------------------------------------------


async def test_manual_adapter_reset_succeeds() -> None:
    calls: list[bool] = []

    async def recover() -> bool:
        calls.append(True)
        return True

    manager = DeviceManager(_settings(), adapter_recover_fn=recover)
    result = await manager.request_adapter_reset()
    assert result == "ok"
    assert calls == [True]


async def test_manual_adapter_reset_reports_failure() -> None:
    async def recover() -> bool:
        return False

    manager = DeviceManager(_settings(), adapter_recover_fn=recover)
    assert await manager.request_adapter_reset() == "failed"


async def test_manual_adapter_reset_not_configured_without_recover_fn() -> None:
    manager = _make_manager()
    assert await manager.request_adapter_reset() == "not_configured"


async def test_manual_adapter_reset_cools_down_on_repeat_calls() -> None:
    """A second manual reset within _MANUAL_RECOVERY_COOLDOWN is suppressed —
    debounce against an accidental double-click, not a real recovery attempt."""
    calls: list[bool] = []

    async def recover() -> bool:
        calls.append(True)
        return True

    manager = DeviceManager(_settings(), adapter_recover_fn=recover)
    assert await manager.request_adapter_reset() == "ok"
    assert await manager.request_adapter_reset() == "cooling_down"
    assert calls == [True]  # the second call never reached adapter_recover_fn


async def test_manual_adapter_reset_ignores_exceptions() -> None:
    """Mirrors adapter_recover_fn's own contract: a raised exception collapses
    to "failed", it never propagates to the caller (the Portal action)."""

    async def recover() -> bool:
        raise RuntimeError("dbus boom")

    manager = DeviceManager(_settings(), adapter_recover_fn=recover)
    assert await manager.request_adapter_reset() == "failed"


async def test_manual_adapter_reset_feeds_automatic_cooldown() -> None:
    """A successful manual reset also updates _last_recovery, so the
    automatic wedge-detection path doesn't immediately re-trigger its own
    (900s) cooldown right after — the two paths share one adapter, not one
    cooldown clock each."""

    async def recover() -> bool:
        return True

    manager = DeviceManager(_settings(), adapter_recover_fn=recover)
    assert manager._last_recovery is None
    await manager.request_adapter_reset()
    assert manager._last_recovery is not None


async def test_manual_adapter_reset_resets_failure_counters() -> None:
    """A manual reset clears the dense-failure counters the same way the
    automatic path does — otherwise a manual reset right before the 3rd
    dense failure would leave stale count state around."""

    async def recover() -> bool:
        return True

    manager = DeviceManager(_settings(), adapter_recover_fn=recover)
    await manager._note_connect_failure()
    await manager._note_connect_failure()
    assert manager._connect_failures == 2
    await manager.request_adapter_reset()
    assert manager._connect_failures == 0


async def test_manual_adapter_reset_failure_does_not_feed_automatic_cooldown() -> None:
    """A *failed* manual reset must not grant the automatic wedge detector a
    free 900s pass — only a confirmed success should (see
    test_manual_adapter_reset_feeds_automatic_cooldown). Review feedback on
    #66: the original implementation set _last_recovery unconditionally
    before the attempt."""

    async def recover() -> bool:
        return False

    manager = DeviceManager(_settings(), adapter_recover_fn=recover)
    assert await manager.request_adapter_reset() == "failed"
    assert manager._last_recovery is None


async def test_manual_adapter_reset_failure_does_not_reset_failure_counters() -> None:
    """A failed manual reset must not erase failure counts that still
    reflect a real, unresolved wedge — only a confirmed success clears them
    (see test_manual_adapter_reset_resets_failure_counters)."""

    async def recover() -> bool:
        return False

    manager = DeviceManager(_settings(), adapter_recover_fn=recover)
    await manager._note_connect_failure()
    await manager._note_connect_failure()
    assert await manager.request_adapter_reset() == "failed"
    assert manager._connect_failures == 2


async def test_manual_adapter_reset_exception_does_not_feed_automatic_cooldown() -> None:
    """Same guarantee as the plain-failure case above, for the
    adapter_recover_fn-raised path."""

    async def recover() -> bool:
        raise RuntimeError("dbus boom")

    manager = DeviceManager(_settings(), adapter_recover_fn=recover)
    assert await manager.request_adapter_reset() == "failed"
    assert manager._last_recovery is None


async def test_manual_adapter_reset_serializes_concurrent_calls() -> None:
    """Two POSTs arriving at once must not both invoke adapter_recover_fn —
    the second must observe the first's just-set cooldown and back off,
    even though the first is still awaiting the recovery subprocess when
    the second call begins. Review feedback on #66 (concurrency safety)."""
    calls = 0
    release = asyncio.Event()

    async def recover() -> bool:
        nonlocal calls
        calls += 1
        await release.wait()  # hold the lock open long enough for a real race
        return True

    manager = DeviceManager(_settings(), adapter_recover_fn=recover)
    task_a = asyncio.create_task(manager.request_adapter_reset())
    await asyncio.sleep(0)  # let task_a enter the lock and start recover()
    task_b = asyncio.create_task(manager.request_adapter_reset())
    await asyncio.sleep(0)  # let task_b attempt to enter — must block, not race in

    release.set()
    result_a = await task_a
    result_b = await task_b

    assert calls == 1  # adapter_recover_fn invoked exactly once
    assert {result_a, result_b} == {"ok", "cooling_down"}


async def test_reclaim_recovery_path_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full recovery arc: empty scans trigger a successful reclaim, the
    backoff resets, the next scan finds the speaker, the empty-scan counter
    restarts, and no second reclaim is attempted below the threshold."""
    reclaims: list[bool] = []

    async def reclaim() -> bool:
        reclaims.append(True)
        return True

    found = object()
    results: list[object | None] = [None, None, None, found, None, None]

    async def _scan_script(*_: object, **__: object) -> ScanResult:
        item = results.pop(0)
        return ScanResult(device=item, beacon_seen=item is not None)  # type: ignore[arg-type]

    monkeypatch.setattr("partyboxd.device.manager.Scanner.find_with_presence", _scan_script)
    manager = DeviceManager(_settings(reconnect_delay=1.0), stale_reclaim_fn=reclaim)
    manager._retry_delay = 32.0  # simulate prior backoff growth

    for _ in range(3):
        await manager._scan()
    assert reclaims == [True]
    assert manager._retry_delay == 1.0  # reset so the rescan happens promptly

    # The speaker, freed by the reclaim, is found on the very next scan.
    assert await manager._scan() is found
    assert manager._empty_scans == 0

    # Two further empties stay under the threshold: no second reclaim.
    await manager._scan()
    await manager._scan()
    assert reclaims == [True]


async def test_unsuccessful_reclaim_is_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reclaim that found nothing is not retried within _RECLAIM_COOLDOWN,
    so dense connect failures don't re-enumerate the bus on every one."""
    from partyboxd.device.manager import _RECLAIM_COOLDOWN

    reclaims: list[bool] = []

    async def reclaim() -> bool:
        reclaims.append(True)
        return False

    manager = DeviceManager(_settings(), stale_reclaim_fn=reclaim)
    assert await manager._reclaim_stale_link() is False
    assert await manager._reclaim_stale_link() is False  # inside cool-down
    assert reclaims == [True]
    # Age past the cool-down; the next attempt runs again.
    assert manager._last_failed_reclaim is not None
    manager._last_failed_reclaim -= _RECLAIM_COOLDOWN + 1
    assert await manager._reclaim_stale_link() is False
    assert reclaims == [True, True]


# ---------------------------------------------------------------------------
# "unreachable" state — FDDF beacon proves the speaker is on despite no
# control connection (2026-07-18: live incident, Portal wrongly said "off"
# while the speaker was confirmed on and merely uncontrollable)
# ---------------------------------------------------------------------------


def _snap(**overrides: object) -> StatusSnapshot:
    base: dict[str, object] = {
        "connected": False,
        "address": None,
        "firmware": None,
        "battery": None,
        "battery_status": None,
        "has_battery": False,
        "speaker_awake": True,
        "beacon_seen": False,
    }
    base.update(overrides)
    return StatusSnapshot(**base)  # type: ignore[arg-type]


def test_speaker_state_off_when_disconnected_and_no_beacon() -> None:
    assert _snap(connected=False, beacon_seen=False).speaker_state == "off"


def test_speaker_state_unreachable_when_disconnected_but_beacon_seen() -> None:
    assert _snap(connected=False, beacon_seen=True).speaker_state == "unreachable"


def test_speaker_state_ignores_beacon_seen_while_connected() -> None:
    """beacon_seen must not influence standby/on classification — it is
    only consulted while disconnected."""
    assert _snap(connected=True, speaker_awake=True, beacon_seen=False).speaker_state == "on"
    assert _snap(connected=True, speaker_awake=False, beacon_seen=False).speaker_state == "standby"


async def test_scan_updates_beacon_seen_with_no_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The core new capability: a scan that finds no connectable candidate
    but does see the beacon must move speaker_state from 'off' to
    'unreachable', not leave it at 'off'."""

    async def _beacon_only(*_: object, **__: object) -> ScanResult:
        return ScanResult(device=None, beacon_seen=True)

    monkeypatch.setattr("partyboxd.device.manager.Scanner.find_with_presence", _beacon_only)
    manager = _make_manager()
    assert manager.snapshot.speaker_state == "off"
    assert await manager._scan() is None
    assert manager.snapshot.speaker_state == "unreachable"


async def test_scan_reverts_to_off_when_beacon_no_longer_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = [
        ScanResult(device=None, beacon_seen=True),
        ScanResult(device=None, beacon_seen=False),
    ]

    async def _scripted(*_: object, **__: object) -> ScanResult:
        return results.pop(0)

    monkeypatch.setattr("partyboxd.device.manager.Scanner.find_with_presence", _scripted)
    manager = _make_manager()
    await manager._scan()
    assert manager.snapshot.speaker_state == "unreachable"
    await manager._scan()
    assert manager.snapshot.speaker_state == "off"


async def test_unreachable_transition_emits_speaker_state_changed_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _beacon_only(*_: object, **__: object) -> ScanResult:
        return ScanResult(device=None, beacon_seen=True)

    monkeypatch.setattr("partyboxd.device.manager.Scanner.find_with_presence", _beacon_only)
    manager = _make_manager()
    queue = manager.subscribe()
    await manager._scan()
    event = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert isinstance(event, SpeakerStateChangedEvent)
    assert event.state == "unreachable"


async def test_scan_error_does_not_touch_beacon_seen(monkeypatch: pytest.MonkeyPatch) -> None:
    """A scan that outright fails (adapter unusable) says nothing about
    presence — unlike a clean empty scan, it must not report beacon_seen at
    all, so the snapshot's prior value is left alone."""

    async def _broken(*_: object, **__: object) -> ScanResult:
        raise RuntimeError("org.bluez.Error.NotReady")

    manager = _make_manager()
    monkeypatch.setattr("partyboxd.device.manager.Scanner.find_with_presence", _broken)
    assert await manager._scan() is None
    assert manager.snapshot.beacon_seen is False  # unchanged from initial default
