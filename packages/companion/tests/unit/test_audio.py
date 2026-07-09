"""Unit tests for AudioService.

No Bluetooth hardware or bluetoothctl binary is required. Tests cover:
- run() waits for update_address() when no address is configured
- status reflects connection state
- run() connects when the sink is not connected
- run() skips connect when already connected
- Clean cancellation
- _is_connected() parsing of bluetoothctl output
- _connect() handles timeout and OSError gracefully
- AudioSettings defaults
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from companion.config import AudioSettings
from companion.services._a2dp_connect import STALE_BOND_CODE, error_code
from companion.services.audio import (
    _FAILURE_COOLDOWN,
    _FAILURE_LIMIT,
    _FLAP_COOLDOWN,
    _FLAP_LIMIT,
    _FLAP_WINDOW,
    AudioReadyChanged,
    AudioService,
    AudioStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _service(address: str | None = "AA:BB:CC:DD:EE:FF") -> AudioService:
    return AudioService(AudioSettings(sink_address=address))


def _mock_proc(stdout: bytes = b"", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = MagicMock()
    return proc


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_initially_not_connected() -> None:
    svc = _service()
    assert svc.status == AudioStatus(connected=False, address="AA:BB:CC:DD:EE:FF")


def test_status_no_address() -> None:
    svc = _service(address=None)
    assert svc.status == AudioStatus(connected=False, address=None)


# ---------------------------------------------------------------------------
# run() — no address: waits for update_address()
# ---------------------------------------------------------------------------


async def test_run_waits_when_no_address() -> None:
    """run() must block (not return) when no address is configured."""
    svc = _service(address=None)
    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0)  # let run() reach await self._address_ready.wait()
    assert not task.done()  # still suspended — proved it's waiting
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def test_update_address_sets_value_and_wakes_run() -> None:
    """update_address() persists the address and unblocks a waiting run()."""
    svc = _service(address=None)
    assert svc.status.address is None

    entered_loop = asyncio.Event()

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        entered_loop.set()
        return _mock_proc(b"Connected: no")

    async def fake_wait_retry(delay: float) -> bool:
        raise asyncio.CancelledError

    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0)  # run() is waiting for address

    with (
        patch("companion.services.audio.asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch.object(svc, "_wait_retry", side_effect=fake_wait_retry),
    ):
        svc.update_address("BB:CC:DD:EE:FF:00")
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2.0)

    assert svc.status.address == "BB:CC:DD:EE:FF:00"
    assert entered_loop.is_set()


# ---------------------------------------------------------------------------
# run() — connect/skip logic
# ---------------------------------------------------------------------------


async def test_run_connects_when_not_connected() -> None:
    svc = _service()

    # Three subprocess calls per failed iteration:
    # _is_connected() → _connect() → _is_connected() post-connect
    call_results = [_mock_proc(b"false"), _mock_proc(b""), _mock_proc(b"false")]

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        return call_results.pop(0)

    retry_calls: list[float] = []

    async def fake_wait_retry(delay: float) -> bool:
        retry_calls.append(delay)
        raise asyncio.CancelledError

    with (
        patch("companion.services.audio.asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch.object(svc, "_wait_retry", side_effect=fake_wait_retry),
    ):
        with pytest.raises(asyncio.CancelledError):
            await svc.run()

    assert len(retry_calls) == 1  # waited once after connect attempt


async def test_run_settle_sleep_after_successful_connect() -> None:
    """After _connect() succeeds, _POST_CONNECT_SETTLE sleep fires before next check."""
    svc = _service()
    events = svc.subscribe()

    # Iteration 1: _is_connected() → False, _connect() → ok (b"ok"), settle sleep → cancel
    call_results = [_mock_proc(b"false"), _mock_proc(b"ok")]

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        return call_results.pop(0)

    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        raise asyncio.CancelledError

    with (
        patch("companion.services.audio.asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch("companion.services.audio.asyncio.sleep", side_effect=fake_sleep),
    ):
        with pytest.raises(asyncio.CancelledError):
            await svc.run()

    from companion.services.audio import _POST_CONNECT_SETTLE, AudioReadyChanged

    assert sleep_calls == [_POST_CONNECT_SETTLE]
    # subscribe() pre-populates with initial state; collect all events emitted
    emitted = []
    while not events.empty():
        emitted.append(events.get_nowait())
    # audio_ready=True was emitted before the settle sleep (and False after cancellation)
    assert AudioReadyChanged(audio_ready=True, address="AA:BB:CC:DD:EE:FF") in emitted


async def test_run_backoff_doubles_on_repeated_failures() -> None:
    """retry_delay doubles after each failed connect cycle."""
    svc = _service()

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        return _mock_proc(b"Connected: no")

    retry_calls: list[float] = []
    call_count = 0

    async def fake_wait_retry(delay: float) -> bool:
        nonlocal call_count
        retry_calls.append(delay)
        call_count += 1
        if call_count >= 3:
            raise asyncio.CancelledError
        return False  # timed out — backoff should increase

    with (
        patch("companion.services.audio.asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch.object(svc, "_wait_retry", side_effect=fake_wait_retry),
    ):
        with pytest.raises(asyncio.CancelledError):
            await svc.run()

    assert retry_calls[0] == 10.0
    assert retry_calls[1] == 20.0
    assert retry_calls[2] == 40.0


async def test_run_backoff_resets_on_success() -> None:
    """retry_delay resets to base once connection is stable."""
    svc = _service()

    # Each failed iteration: _is_connected() → _connect() → _is_connected() post-connect
    # Successful iteration: _is_connected() → True → sleep(_CHECK_INTERVAL)
    responses = [
        _mock_proc(b"false"),  # _is_connected() #1 → not connected
        _mock_proc(b""),  # _connect() #1 → fail
        _mock_proc(b"false"),  # _is_connected() post-connect #1 → still not connected
        _mock_proc(b"false"),  # _is_connected() #2 → not connected
        _mock_proc(b""),  # _connect() #2 → fail
        _mock_proc(b"false"),  # _is_connected() post-connect #2 → still not connected
        _mock_proc(b"true"),  # _is_connected() #3 → connected → audio_ready=True
    ]

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        return responses.pop(0)

    retry_calls: list[float] = []

    async def fake_wait_retry(delay: float) -> bool:
        retry_calls.append(delay)
        if len(retry_calls) >= 3:
            raise asyncio.CancelledError
        return False  # simulate timeout (not woken by re-pair)

    with (
        patch("companion.services.audio.asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch.object(svc, "_wait_retry", side_effect=fake_wait_retry),
    ):
        with pytest.raises(asyncio.CancelledError):
            await svc.run()

    # Two retry waits (10s, 20s), then _CHECK_INTERVAL (60s) after success —
    # the connected-idle wait is interruptible (see recheck_now()), so it now
    # goes through _wait_retry too, not a bare asyncio.sleep.
    assert retry_calls == [10.0, 20.0, 60.0]


async def test_run_update_address_resets_backoff() -> None:
    """update_address() while sleeping resets retry_delay to base."""
    svc = _service()

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        return _mock_proc(b"Connected: no")

    retry_calls: list[float] = []
    call_count = 0

    async def fake_wait_retry(delay: float) -> bool:
        nonlocal call_count
        retry_calls.append(delay)
        call_count += 1
        if call_count == 1:
            return False  # first retry: timed out → backoff doubles to 20s
        if call_count == 2:
            return True  # second retry: woken by re-pair → backoff resets
        raise asyncio.CancelledError

    with (
        patch("companion.services.audio.asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch.object(svc, "_wait_retry", side_effect=fake_wait_retry),
    ):
        with pytest.raises(asyncio.CancelledError):
            await svc.run()

    assert retry_calls[0] == 10.0  # initial delay
    assert retry_calls[1] == 20.0  # doubled after timeout
    assert retry_calls[2] == 10.0  # reset after re-pair wakeup


async def test_recheck_now_wakes_a_pending_wait_retry() -> None:
    """recheck_now() interrupts _wait_retry immediately instead of waiting out
    its full delay — the mechanism _recheck_audio_on_standby (companion.
    __main__) depends on to avoid a stale "connected" status for up to 60s
    after the speaker leaves standby (ADR-034)."""
    svc = _service()

    wait_task = asyncio.create_task(svc._wait_retry(60.0))
    await asyncio.sleep(0)  # let _wait_retry start waiting on _reconnect_now

    svc.recheck_now()

    woke_early = await asyncio.wait_for(wait_task, timeout=1.0)
    assert woke_early is True


async def test_run_skips_connect_when_already_connected() -> None:
    svc = _service()

    connected_proc = _mock_proc(b"true")

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        return connected_proc

    connect_called = False
    original_connect = svc._connect

    async def spy_connect() -> None:
        nonlocal connect_called
        connect_called = True
        await original_connect()

    svc._connect = spy_connect  # type: ignore[method-assign]

    async def fake_wait_retry(delay: float) -> bool:
        raise asyncio.CancelledError

    with patch("companion.services.audio.asyncio.create_subprocess_exec", side_effect=fake_exec):
        with patch.object(svc, "_wait_retry", side_effect=fake_wait_retry):
            with pytest.raises(asyncio.CancelledError):
                await svc.run()

    assert not connect_called


# ---------------------------------------------------------------------------
# Clean cancellation
# ---------------------------------------------------------------------------


async def test_run_cancels_cleanly() -> None:
    svc = _service()

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        return _mock_proc(b"true")

    async def fake_wait_retry(delay: float) -> bool:
        raise asyncio.CancelledError

    with patch("companion.services.audio.asyncio.create_subprocess_exec", side_effect=fake_exec):
        with patch.object(svc, "_wait_retry", side_effect=fake_wait_retry):
            task = asyncio.create_task(svc.run())
            with pytest.raises(asyncio.CancelledError):
                await task


# ---------------------------------------------------------------------------
# _is_connected()
# ---------------------------------------------------------------------------


async def test_is_connected_true() -> None:
    svc = _service()
    with patch(
        "companion.services.audio.asyncio.create_subprocess_exec",
        return_value=_mock_proc(b"true"),
    ):
        assert await svc._is_connected() is True


async def test_is_connected_false() -> None:
    svc = _service()
    with patch(
        "companion.services.audio.asyncio.create_subprocess_exec",
        return_value=_mock_proc(b"false"),
    ):
        assert await svc._is_connected() is False


async def test_is_connected_handles_oserror() -> None:
    svc = _service()
    with patch(
        "companion.services.audio.asyncio.create_subprocess_exec",
        side_effect=OSError("bluetoothctl not found"),
    ):
        assert await svc._is_connected() is False


async def test_is_connected_handles_timeout() -> None:
    svc = _service()

    async def slow_communicate() -> tuple[bytes, bytes]:
        raise TimeoutError

    proc = MagicMock()
    proc.communicate = slow_communicate
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    with patch(
        "companion.services.audio.asyncio.create_subprocess_exec",
        return_value=proc,
    ):
        assert await svc._is_connected() is False


# ---------------------------------------------------------------------------
# _connect()
# ---------------------------------------------------------------------------


async def test_connect_succeeds() -> None:
    svc = _service()
    with patch(
        "companion.services.audio.asyncio.create_subprocess_exec",
        return_value=_mock_proc(b"Connection successful\n"),
    ):
        await svc._connect()  # must not raise


async def test_connect_logs_failure_on_error_output() -> None:
    """_connect() warns when bluetoothctl output contains 'Failed to connect'."""
    svc = _service()
    with patch(
        "companion.services.audio.asyncio.create_subprocess_exec",
        return_value=_mock_proc(b"Failed to connect: org.bluez.Error.Failed\n"),
    ):
        await svc._connect()  # must not raise


async def test_connect_handles_oserror() -> None:
    svc = _service()
    with patch(
        "companion.services.audio.asyncio.create_subprocess_exec",
        side_effect=OSError("bluetoothctl missing"),
    ):
        await svc._connect()  # must not raise


async def test_connect_handles_timeout() -> None:
    svc = _service()

    proc = MagicMock()
    proc.returncode = None
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    proc.communicate = AsyncMock(side_effect=TimeoutError)
    with patch(
        "companion.services.audio.asyncio.create_subprocess_exec",
        return_value=proc,
    ):
        await svc._connect()  # must not raise


# ---------------------------------------------------------------------------
# Subprocess error protocol (error_code) + stale-bond classification
# ---------------------------------------------------------------------------


def test_error_code_returns_none_for_non_error_lines() -> None:
    assert error_code("ok") is None
    assert error_code("true") is None
    assert error_code("false") is None
    assert error_code("") is None


def test_error_code_extracts_known_code() -> None:
    assert error_code(f"err:{STALE_BOND_CODE}:device unknown to BlueZ") == STALE_BOND_CODE
    # Detail is optional — the code token alone still classifies.
    assert error_code(f"err:{STALE_BOND_CODE}") == STALE_BOND_CODE


def test_error_code_ignores_unknown_or_freetext_tokens() -> None:
    # Free-text failures (no registered code) must not be mistaken for a code,
    # even when the text happens to contain a colon.
    assert error_code("err:br-connection-unknown") is None
    assert error_code("err:org.bluez.Error.Failed: some detail") is None


async def test_connect_skips_disconnect_on_stale_bond() -> None:
    """A stale bond has no Device1 to disconnect — cleanup must be skipped."""
    svc = _service()
    svc._disconnect = AsyncMock()  # type: ignore[method-assign]
    with patch(
        "companion.services.audio.asyncio.create_subprocess_exec",
        return_value=_mock_proc(f"err:{STALE_BOND_CODE}:device unknown\n".encode()),
    ):
        assert await svc._connect() is False
    svc._disconnect.assert_not_called()


async def test_connect_disconnects_on_generic_failure() -> None:
    """A generic connect failure still triggers the cleanup disconnect."""
    svc = _service()
    svc._disconnect = AsyncMock()  # type: ignore[method-assign]
    with patch(
        "companion.services.audio.asyncio.create_subprocess_exec",
        return_value=_mock_proc(b"err:org.bluez.Error.Failed\n"),
    ):
        assert await svc._connect() is False
    svc._disconnect.assert_awaited_once()


# ---------------------------------------------------------------------------
# AudioSettings
# ---------------------------------------------------------------------------


def test_audio_settings_default_no_address() -> None:
    s = AudioSettings()
    assert s.sink_address is None


def test_audio_settings_accepts_address() -> None:
    s = AudioSettings(sink_address="50:1B:6A:14:FD:1D")
    assert s.sink_address == "50:1B:6A:14:FD:1D"


# ---------------------------------------------------------------------------
# audio_ready property
# ---------------------------------------------------------------------------


def test_audio_ready_initially_false() -> None:
    svc = _service()
    assert svc.audio_ready is False


def test_audio_ready_reflects_status_connected() -> None:
    """status.connected and audio_ready are always in sync."""
    svc = _service()
    assert svc.status.connected is svc.audio_ready
    svc._set_audio_ready(True)
    assert svc.status.connected is svc.audio_ready


# ---------------------------------------------------------------------------
# forget() — factory reset
# ---------------------------------------------------------------------------


def test_forget_clears_address_and_marks_not_ready() -> None:
    """forget() un-sets the sink and drops audio_ready (factory reset)."""
    svc = _service(address="AA:BB:CC:DD:EE:FF")
    svc._set_audio_ready(True)
    queue = svc.subscribe()
    queue.get_nowait()  # consume the initial-state event (True)

    svc.forget()

    assert svc.status.address is None
    assert svc.audio_ready is False
    # The address_ready gate is re-armed so run() returns to waiting for pairing.
    assert not svc._address_ready.is_set()
    assert queue.get_nowait() == AudioReadyChanged(audio_ready=False)


# ---------------------------------------------------------------------------
# AudioReadyChanged events — _set_audio_ready transitions
# ---------------------------------------------------------------------------


async def test_set_audio_ready_emits_event_on_false_to_true() -> None:
    svc = _service()
    queue = svc.subscribe()
    queue.get_nowait()  # consume the initial-state event (False)

    svc._set_audio_ready(False)  # False → False: no event
    assert queue.empty()

    svc._set_audio_ready(True)  # False → True: event
    assert not queue.empty()
    assert queue.get_nowait() == AudioReadyChanged(audio_ready=True, address="AA:BB:CC:DD:EE:FF")


async def test_set_audio_ready_emits_event_on_true_to_false() -> None:
    svc = _service()
    queue = svc.subscribe()
    queue.get_nowait()  # consume the initial-state event (False)
    svc._set_audio_ready(True)
    queue.get_nowait()  # consume the True transition event

    svc._set_audio_ready(False)  # True → False: event
    assert not queue.empty()
    assert queue.get_nowait() == AudioReadyChanged(audio_ready=False, address="AA:BB:CC:DD:EE:FF")


async def test_set_audio_ready_no_event_when_unchanged() -> None:
    """No event is emitted when the value does not change."""
    svc = _service()
    queue = svc.subscribe()
    queue.get_nowait()  # consume the initial-state event (False)

    svc._set_audio_ready(False)  # False → False
    svc._set_audio_ready(False)  # still False
    assert queue.empty()

    svc._set_audio_ready(True)
    queue.get_nowait()

    svc._set_audio_ready(True)  # True → True
    assert queue.empty()


# ---------------------------------------------------------------------------
# subscribe / unsubscribe
# ---------------------------------------------------------------------------


async def test_subscribe_delivers_current_state_immediately() -> None:
    """subscribe() pre-populates the queue with the current state."""
    svc = _service()
    # Not ready at construction time
    queue = svc.subscribe()
    assert not queue.empty()
    assert queue.get_nowait() == AudioReadyChanged(audio_ready=False, address="AA:BB:CC:DD:EE:FF")

    # Now set ready and subscribe again
    svc._set_audio_ready(True)
    queue2 = svc.subscribe()
    assert queue2.get_nowait() == AudioReadyChanged(audio_ready=True, address="AA:BB:CC:DD:EE:FF")


async def test_subscribe_returns_queue_that_receives_events() -> None:
    svc = _service()
    queue = svc.subscribe()
    queue.get_nowait()  # drain initial-state event

    svc._set_audio_ready(True)
    assert not queue.empty()
    assert queue.get_nowait().audio_ready is True


async def test_multiple_subscribers_each_receive_events() -> None:
    svc = _service()
    q1 = svc.subscribe()
    q2 = svc.subscribe()
    q1.get_nowait()  # drain initial-state events
    q2.get_nowait()

    svc._set_audio_ready(True)
    assert q1.get_nowait() == AudioReadyChanged(audio_ready=True, address="AA:BB:CC:DD:EE:FF")
    assert q2.get_nowait() == AudioReadyChanged(audio_ready=True, address="AA:BB:CC:DD:EE:FF")


async def test_unsubscribe_stops_delivery() -> None:
    svc = _service()
    queue = svc.subscribe()
    queue.get_nowait()  # drain initial-state event
    svc.unsubscribe(queue)

    svc._set_audio_ready(True)
    assert queue.empty()


async def test_unsubscribe_unknown_queue_is_idempotent() -> None:
    """Calling unsubscribe with a queue that was never subscribed must not raise."""
    svc = _service()
    import asyncio

    orphan: asyncio.Queue[object] = asyncio.Queue()
    svc.unsubscribe(orphan)  # type: ignore[arg-type]  # must not raise


# ---------------------------------------------------------------------------
# audio_ready during run() lifecycle
# ---------------------------------------------------------------------------


async def test_run_emits_audio_ready_true_on_connect() -> None:
    """audio_ready transitions True when run() detects a connected A2DP sink."""
    svc = _service()
    queue = svc.subscribe()

    responses = [
        _mock_proc(b"true"),  # _is_connected() → connected
    ]

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        return responses.pop(0)

    async def fake_wait_retry(delay: float) -> bool:
        raise asyncio.CancelledError

    with patch("companion.services.audio.asyncio.create_subprocess_exec", side_effect=fake_exec):
        with patch.object(svc, "_wait_retry", side_effect=fake_wait_retry):
            with pytest.raises(asyncio.CancelledError):
                await svc.run()

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())

    assert AudioReadyChanged(audio_ready=True, address="AA:BB:CC:DD:EE:FF") in events


async def test_run_emits_audio_ready_false_on_cancel() -> None:
    """audio_ready transitions False when run() is cancelled while connected."""
    svc = _service()

    call_count = 0

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        return _mock_proc(b"true")

    async def fake_wait_retry(delay: float) -> bool:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise asyncio.CancelledError
        return False

    with patch("companion.services.audio.asyncio.create_subprocess_exec", side_effect=fake_exec):
        with patch.object(svc, "_wait_retry", side_effect=fake_wait_retry):
            with pytest.raises(asyncio.CancelledError):
                await svc.run()

    assert svc.audio_ready is False


# ---------------------------------------------------------------------------
# Flap detection — repeated short-lived connections trigger a cooldown
# ---------------------------------------------------------------------------


async def test_set_audio_ready_counts_short_connection_as_flap() -> None:
    """A connection that drops within _FLAP_WINDOW increments the flap count."""
    svc = _service()
    times = iter([0.0, 1.0])  # connected at t=0, dropped at t=1 (< _FLAP_WINDOW)
    with patch("companion.services.audio.time.monotonic", side_effect=lambda: next(times)):
        svc._set_audio_ready(True)
        svc._set_audio_ready(False)
    assert svc._flap_count == 1


async def test_set_audio_ready_flap_count_resets_after_stable_connection() -> None:
    """A connection that survives past _FLAP_WINDOW resets any prior flap count."""
    svc = _service()
    svc._flap_count = 2  # simulate two prior flaps
    times = iter([0.0, _FLAP_WINDOW + 1.0])  # connected at t=0, dropped well after window
    with patch("companion.services.audio.time.monotonic", side_effect=lambda: next(times)):
        svc._set_audio_ready(True)
        svc._set_audio_ready(False)
    assert svc._flap_count == 0


async def test_run_enters_cooldown_when_flap_limit_reached() -> None:
    """run() waits _FLAP_COOLDOWN instead of reconnecting immediately once flapping."""
    svc = _service()
    svc._audio_ready = True
    svc._connected_at = time.monotonic()
    svc._flap_count = _FLAP_LIMIT - 1  # one more short-lived drop trips the limit

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        return _mock_proc(b"false")  # _is_connected() -> not connected

    retry_calls: list[float] = []

    async def fake_wait_retry(delay: float) -> bool:
        retry_calls.append(delay)
        raise asyncio.CancelledError

    with (
        patch("companion.services.audio.asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch.object(svc, "_wait_retry", side_effect=fake_wait_retry),
    ):
        with pytest.raises(asyncio.CancelledError):
            await svc.run()

    assert retry_calls == [_FLAP_COOLDOWN]
    assert svc._flap_count == 0


# ---------------------------------------------------------------------------
# Sustained-failure detection — outright connect failures (never reaching
# audio_ready=True) trigger their own cooldown, independent of flap detection
# ---------------------------------------------------------------------------


async def test_consecutive_failures_increments_on_outright_connect_failure() -> None:
    """An outright connect failure (ConnectProfile never succeeds) increments
    the separate consecutive-failure counter — flap detection never fires here
    because audio_ready never transitions to True in the first place."""
    svc = _service()

    # _is_connected() -> false, _connect() -> fail, _is_connected() post-check -> false
    call_results = [_mock_proc(b"false"), _mock_proc(b""), _mock_proc(b"false")]

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        return call_results.pop(0)

    async def fake_wait_retry(delay: float) -> bool:
        raise asyncio.CancelledError

    with (
        patch("companion.services.audio.asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch.object(svc, "_wait_retry", side_effect=fake_wait_retry),
    ):
        with pytest.raises(asyncio.CancelledError):
            await svc.run()

    assert svc._consecutive_failures == 1
    assert svc._flap_count == 0


async def test_consecutive_failures_resets_on_successful_connect() -> None:
    """A successful ConnectProfile clears the consecutive-failure counter."""
    svc = _service()
    svc._consecutive_failures = 3

    call_results = [_mock_proc(b"false"), _mock_proc(b"ok")]

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        return call_results.pop(0)

    async def fake_sleep(delay: float) -> None:
        raise asyncio.CancelledError

    with (
        patch("companion.services.audio.asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch("companion.services.audio.asyncio.sleep", side_effect=fake_sleep),
    ):
        with pytest.raises(asyncio.CancelledError):
            await svc.run()

    assert svc._consecutive_failures == 0


async def test_run_enters_cooldown_when_consecutive_failures_reach_limit() -> None:
    """run() waits _FAILURE_COOLDOWN once outright connect failures accumulate
    to _FAILURE_LIMIT — the gap the flap cooldown doesn't cover, since
    audio_ready never went True during a sustained outright-failure loop."""
    svc = _service()
    svc._consecutive_failures = _FAILURE_LIMIT - 1  # one more failure trips the limit

    call_results = [_mock_proc(b"false"), _mock_proc(b""), _mock_proc(b"false")]

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        return call_results.pop(0)

    retry_calls: list[float] = []

    async def fake_wait_retry(delay: float) -> bool:
        retry_calls.append(delay)
        raise asyncio.CancelledError

    with (
        patch("companion.services.audio.asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch.object(svc, "_wait_retry", side_effect=fake_wait_retry),
    ):
        with pytest.raises(asyncio.CancelledError):
            await svc.run()

    assert retry_calls == [_FAILURE_COOLDOWN]
    assert svc._consecutive_failures == 0
