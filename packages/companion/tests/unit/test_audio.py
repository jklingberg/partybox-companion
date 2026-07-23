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
    _STANDBY_RECHECK,
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

    async def fake_wait_retry(delay: float, *, interrupt_on_recheck: bool = False) -> bool:
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

    async def fake_wait_retry(delay: float, *, interrupt_on_recheck: bool = False) -> bool:
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

    async def fake_wait_retry(delay: float, *, interrupt_on_recheck: bool = False) -> bool:
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

    async def fake_wait_retry(delay: float, *, interrupt_on_recheck: bool = False) -> bool:
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

    async def fake_wait_retry(delay: float, *, interrupt_on_recheck: bool = False) -> bool:
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


async def test_recheck_now_wakes_a_pending_idle_wait() -> None:
    """recheck_now() interrupts the connected-idle wait immediately instead of
    waiting out its full delay — the mechanism _recheck_audio_on_standby
    (companion.__main__) depends on to avoid a stale "connected" status for up
    to 60s after the speaker leaves standby (ADR-034). It reports False (not a
    re-pair): only the idle wait opts in via interrupt_on_recheck, so failure
    backoffs stay immune to recheck nudges (see recheck_now's docstring)."""
    svc = _service()

    wait_task = asyncio.create_task(svc._wait_retry(60.0, interrupt_on_recheck=True))
    await asyncio.sleep(0)  # let _wait_retry start waiting on its events

    svc.recheck_now()

    woke_by_repair = await asyncio.wait_for(wait_task, timeout=1.0)
    assert woke_by_repair is False


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

    async def fake_wait_retry(delay: float, *, interrupt_on_recheck: bool = False) -> bool:
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

    async def fake_wait_retry(delay: float, *, interrupt_on_recheck: bool = False) -> bool:
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
# transport_active()
# ---------------------------------------------------------------------------


async def test_transport_active_true_when_streaming() -> None:
    svc = _service()
    svc._audio_ready = True
    with patch(
        "companion.services.audio.asyncio.create_subprocess_exec",
        return_value=_mock_proc(b"active"),
    ):
        assert await svc.transport_active() is True


async def test_transport_active_pending_counts_as_streaming() -> None:
    svc = _service()
    svc._audio_ready = True
    with patch(
        "companion.services.audio.asyncio.create_subprocess_exec",
        return_value=_mock_proc(b"pending"),
    ):
        assert await svc.transport_active() is True


async def test_transport_active_false_when_idle() -> None:
    svc = _service()
    svc._audio_ready = True
    with patch(
        "companion.services.audio.asyncio.create_subprocess_exec",
        return_value=_mock_proc(b"idle"),
    ):
        assert await svc.transport_active() is False


async def test_transport_active_short_circuits_when_not_ready() -> None:
    svc = _service()  # audio_ready is False until run() connects
    with patch("companion.services.audio.asyncio.create_subprocess_exec") as exec_mock:
        assert await svc.transport_active() is False
    exec_mock.assert_not_called()


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
# pin_volume_fn — ARCH-04/INC-2: pin the sink to 100% on a fresh A2DP connect
# ---------------------------------------------------------------------------


async def test_pin_volume_fn_called_once_on_fresh_connect() -> None:
    """The injected actuator fires exactly once per False→True transition —
    not on every _CHECK_INTERVAL confirmation of an already-stable link."""
    pin_calls = 0

    async def pin() -> None:
        nonlocal pin_calls
        pin_calls += 1

    svc = AudioService(AudioSettings(sink_address="AA:BB:CC:DD:EE:FF"), pin_volume_fn=pin)

    call_count = 0

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        return _mock_proc(b"true")  # already connected every time

    async def fake_wait_retry(delay: float, *, interrupt_on_recheck: bool = False) -> bool:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise asyncio.CancelledError
        return False

    with (
        patch("companion.services.audio.asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch.object(svc, "_wait_retry", side_effect=fake_wait_retry),
    ):
        with pytest.raises(asyncio.CancelledError):
            await svc.run()

    assert pin_calls == 1


async def test_pin_volume_fn_called_after_explicit_connect() -> None:
    """The actuator also fires on the _connect() success path, not only the
    "already connected at startup" branch covered above — and only after the
    settle sleep, since the PipeWire sink node isn't guaranteed to exist
    until MediaTransport1 shows up (same race the settle sleep itself
    guards against)."""
    pin_calls = 0

    async def pin() -> None:
        nonlocal pin_calls
        pin_calls += 1

    svc = AudioService(AudioSettings(sink_address="AA:BB:CC:DD:EE:FF"), pin_volume_fn=pin)

    call_results = [_mock_proc(b"false"), _mock_proc(b"ok")]

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        if not call_results:
            raise asyncio.CancelledError
        return call_results.pop(0)

    sleep_calls = 0

    async def fake_sleep(delay: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1

    with (
        patch("companion.services.audio.asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch("companion.services.audio.asyncio.sleep", side_effect=fake_sleep),
    ):
        with pytest.raises(asyncio.CancelledError):
            await svc.run()

    assert sleep_calls == 1
    assert pin_calls == 1


async def test_pin_volume_fn_not_called_without_transition() -> None:
    """No actuator, no crash — and _set_audio_ready(True) called again with
    no state change must not re-invoke it."""
    svc = _service()
    svc._audio_ready = True  # already ready
    assert svc._set_audio_ready(True) is False  # not a transition


async def test_pin_volume_failure_does_not_crash_run_loop() -> None:
    """A failing actuator (PipeWire not ready, wpctl missing) is swallowed —
    the connect loop must still reach audio_ready=True rather than propagate
    the error out of run() (cancellation afterwards always flips it back to
    False, per the existing CancelledError handler — that's unrelated to
    the actuator and is asserted elsewhere)."""

    async def failing_pin() -> None:
        raise RuntimeError("wpctl not found")

    svc = AudioService(AudioSettings(sink_address="AA:BB:CC:DD:EE:FF"), pin_volume_fn=failing_pin)
    queue = svc.subscribe()

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        return _mock_proc(b"true")

    async def fake_wait_retry(delay: float, *, interrupt_on_recheck: bool = False) -> bool:
        raise asyncio.CancelledError

    with (
        patch("companion.services.audio.asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch.object(svc, "_wait_retry", side_effect=fake_wait_retry),
    ):
        with pytest.raises(asyncio.CancelledError):
            await svc.run()

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert AudioReadyChanged(audio_ready=True, address="AA:BB:CC:DD:EE:FF") in events


def test_set_audio_ready_returns_true_on_transition() -> None:
    svc = _service()
    assert svc._set_audio_ready(True) is True
    assert svc._set_audio_ready(True) is False  # already ready — not a transition
    assert svc._set_audio_ready(False) is True


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

    async def fake_wait_retry(delay: float, *, interrupt_on_recheck: bool = False) -> bool:
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

    async def fake_wait_retry(delay: float, *, interrupt_on_recheck: bool = False) -> bool:
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

    async def fake_wait_retry(delay: float, *, interrupt_on_recheck: bool = False) -> bool:
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

    async def fake_wait_retry(delay: float, *, interrupt_on_recheck: bool = False) -> bool:
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

    async def fake_wait_retry(delay: float, *, interrupt_on_recheck: bool = False) -> bool:
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


# ---------------------------------------------------------------------------
# recheck vs re-pair wake separation (reconnect->hammer->drop loop, 2026-07-18)
# ---------------------------------------------------------------------------


async def test_recheck_does_not_interrupt_failure_backoff() -> None:
    """recheck_now() must NOT wake a failure/cool-down wait: every BLE
    standby/off flap fires a recheck, and letting it through bypassed the
    flap/failure cool-downs and re-hammered a sleeping speaker."""
    svc = _service()
    svc.recheck_now()
    started = asyncio.get_running_loop().time()
    woken_by_repair = await svc._wait_retry(0.1)
    elapsed = asyncio.get_running_loop().time() - started
    assert woken_by_repair is False
    assert elapsed >= 0.09  # slept the full delay despite the recheck nudge


async def test_recheck_interrupts_connected_idle_wait_without_repair_signal() -> None:
    """With interrupt_on_recheck the idle wait ends early, but is not reported as
    a re-pair (True would reset backoff)."""
    svc = _service()

    async def nudge() -> None:
        await asyncio.sleep(0.01)
        svc.recheck_now()

    task = asyncio.create_task(nudge())
    started = asyncio.get_running_loop().time()
    woken_by_repair = await svc._wait_retry(5.0, interrupt_on_recheck=True)
    elapsed = asyncio.get_running_loop().time() - started
    await task
    assert woken_by_repair is False
    assert elapsed < 1.0  # cut short, didn't sleep the full 5s


async def test_update_address_interrupts_any_wait_as_repair() -> None:
    """update_address() (a real re-pair) wakes both wait shapes and reports
    True so callers reset their backoff."""
    svc = _service()

    async def repair() -> None:
        await asyncio.sleep(0.01)
        svc.update_address("AA:BB:CC:DD:EE:FF")

    for kwargs in ({}, {"interrupt_on_recheck": True}):
        task = asyncio.create_task(repair())
        assert await svc._wait_retry(5.0, **kwargs) is True
        await task


async def test_simultaneous_repair_and_recheck_reports_repair() -> None:
    """When both wake signals fire in the same scheduler tick while the wait
    is pending, the re-pair interpretation must win — callers need to reset
    their retry state. Locks in the priority rule against future rewrites
    (e.g. TaskGroup/asyncio.timeout-based reimplementations).

    Note: signals set BEFORE the wait begins are deliberately dropped by the
    clear()-then-wait design ("only a call made after this point can
    interrupt"), so the race worth pinning is both events firing mid-wait.
    """
    svc = _service()
    wait_task = asyncio.create_task(svc._wait_retry(5.0, interrupt_on_recheck=True))
    await asyncio.sleep(0)  # let both event-waiters start

    # Same tick, no yield between them: recheck first, then the re-pair.
    svc.recheck_now()
    svc.update_address("AA:BB:CC:DD:EE:FF")

    assert await asyncio.wait_for(wait_task, timeout=1.0) is True


async def test_retry_now_interrupts_failure_backoff_as_strong_signal() -> None:
    """retry_now() (speaker woke up / re-pair) must cut a failure back-off or
    cool-down short and report True so the caller resets retry state — the
    counterpart to recheck_now(), which must never do this."""
    svc = _service()

    async def wake() -> None:
        await asyncio.sleep(0.01)
        svc.retry_now("speaker woke up")

    task = asyncio.create_task(wake())
    started = asyncio.get_running_loop().time()
    woken_by_retry = await svc._wait_retry(5.0)
    elapsed = asyncio.get_running_loop().time() - started
    await task
    assert woken_by_retry is True
    assert elapsed < 1.0


# ---------------------------------------------------------------------------
# run() — standby gate
# ---------------------------------------------------------------------------


async def test_run_standby_gate_holds_off_connect() -> None:
    """While standby_fn reports the speaker asleep, run() must not page it:
    no _connect() subprocess, just a _STANDBY_RECHECK wait. A page to a
    standby speaker always fails (err:'br-connection-unknown') and each
    attempt emits kernel noise + steals radio time from the BLE link
    (2026-07-18 deep dive)."""
    svc = AudioService(
        AudioSettings(sink_address="AA:BB:CC:DD:EE:FF"), speaker_state_fn=lambda: "standby"
    )

    exec_calls: list[object] = []

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        exec_calls.append(args)
        return _mock_proc(b"false")  # _is_connected() → not connected

    retry_calls: list[float] = []

    async def fake_wait_retry(delay: float, *, interrupt_on_recheck: bool = False) -> bool:
        retry_calls.append(delay)
        raise asyncio.CancelledError

    with (
        patch("companion.services.audio.asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch.object(svc, "_wait_retry", side_effect=fake_wait_retry),
    ):
        with pytest.raises(asyncio.CancelledError):
            await svc.run()

    assert len(exec_calls) == 1  # the connectivity check only — no connect attempt
    assert retry_calls == [_STANDBY_RECHECK]


async def test_run_standby_gate_resets_retry_state() -> None:
    """Entering the standby gate must clear the failure/flap ladders so a
    wake-up retry starts fresh instead of walking into a cool-down — standby
    is an expected state, not a failure."""
    svc = AudioService(
        AudioSettings(sink_address="AA:BB:CC:DD:EE:FF"), speaker_state_fn=lambda: "standby"
    )
    svc._consecutive_failures = _FAILURE_LIMIT - 1
    svc._flap_count = _FLAP_LIMIT

    retry_calls: list[float] = []

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        return _mock_proc(b"false")

    async def fake_wait_retry(delay: float, *, interrupt_on_recheck: bool = False) -> bool:
        retry_calls.append(delay)
        raise asyncio.CancelledError

    with (
        patch("companion.services.audio.asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch.object(svc, "_wait_retry", side_effect=fake_wait_retry),
    ):
        with pytest.raises(asyncio.CancelledError):
            await svc.run()

    # Gate won over the pending flap cool-down and cleared both ladders.
    assert retry_calls == [_STANDBY_RECHECK]
    assert svc._consecutive_failures == 0
    assert svc._flap_count == 0


async def test_run_standby_gate_open_when_awake() -> None:
    """A non-standby state must leave the connect path untouched."""
    svc = AudioService(
        AudioSettings(sink_address="AA:BB:CC:DD:EE:FF"), speaker_state_fn=lambda: "on"
    )

    # check → connect → post-connect check, as in test_run_connects_when_not_connected
    call_results = [_mock_proc(b"false"), _mock_proc(b""), _mock_proc(b"false")]

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        return call_results.pop(0)

    retry_calls: list[float] = []

    async def fake_wait_retry(delay: float, *, interrupt_on_recheck: bool = False) -> bool:
        retry_calls.append(delay)
        raise asyncio.CancelledError

    with (
        patch("companion.services.audio.asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch.object(svc, "_wait_retry", side_effect=fake_wait_retry),
    ):
        with pytest.raises(asyncio.CancelledError):
            await svc.run()

    assert call_results == []  # all three subprocess stages ran
    assert retry_calls == [10.0]  # _RETRY_BASE — the normal failure wait


async def test_run_standby_gate_logs_entry_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The gate logs on entry, not on every 300s re-evaluation — an overnight
    standby must not repeat the hold-off line all night."""
    svc = AudioService(
        AudioSettings(sink_address="AA:BB:CC:DD:EE:FF"), speaker_state_fn=lambda: "standby"
    )

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        return _mock_proc(b"false")

    waits = 0

    async def fake_wait_retry(delay: float, *, interrupt_on_recheck: bool = False) -> bool:
        nonlocal waits
        waits += 1
        if waits >= 3:  # three full gate cycles, then stop
            raise asyncio.CancelledError
        return False

    with (
        patch("companion.services.audio.asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch.object(svc, "_wait_retry", side_effect=fake_wait_retry),
        caplog.at_level("INFO", logger="companion.services.audio"),
    ):
        with pytest.raises(asyncio.CancelledError):
            await svc.run()

    holds = [r for r in caplog.records if "holding off connect attempts" in r.message]
    assert len(holds) == 1  # entered once, re-evaluated silently
