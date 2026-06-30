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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from companion.config import AudioSettings
from companion.services.audio import AudioReadyChanged, AudioService, AudioStatus

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
    from contextlib import suppress

    svc = _service(address=None)
    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0)  # let run() reach await self._address_ready.wait()
    assert not task.done()  # still suspended — proved it's waiting
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def test_update_address_sets_value_and_wakes_run() -> None:
    """update_address() persists the address and unblocks a waiting run()."""
    from contextlib import suppress

    svc = _service(address=None)
    assert svc.status.address is None

    entered_loop = asyncio.Event()

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        entered_loop.set()
        # Return a proc that looks disconnected; sleep patch will cancel the loop
        return _mock_proc(b"Connected: no")

    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0)  # run() is waiting for address

    with (
        patch("companion.services.audio.asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch(
            "companion.services.audio.asyncio.sleep",
            side_effect=asyncio.CancelledError,
        ),
    ):
        svc.update_address("BB:CC:DD:EE:FF:00")
        # run() will resume, enter the loop, call _is_connected(), then sleep (→ cancel)
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2.0)

    assert svc.status.address == "BB:CC:DD:EE:FF:00"
    assert entered_loop.is_set()


# ---------------------------------------------------------------------------
# run() — connect/skip logic
# ---------------------------------------------------------------------------


async def test_run_connects_when_not_connected() -> None:
    svc = _service()

    connected_proc = _mock_proc(b"Connected: no")
    connect_proc = _mock_proc()

    call_results = [connected_proc, connect_proc]

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        return call_results.pop(0)

    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        raise asyncio.CancelledError

    with patch("companion.services.audio.asyncio.create_subprocess_exec", side_effect=fake_exec):
        with patch("companion.services.audio.asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await svc.run()

    assert len(sleep_calls) == 1  # slept after connect attempt


async def test_run_backoff_doubles_on_repeated_failures() -> None:
    """retry_delay doubles after each failed connect cycle."""
    svc = _service()

    # Always not connected
    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        return _mock_proc(b"Connected: no")

    sleep_calls: list[float] = []
    call_count = 0

    async def fake_sleep(delay: float) -> None:
        nonlocal call_count
        sleep_calls.append(delay)
        call_count += 1
        if call_count >= 3:
            raise asyncio.CancelledError

    with patch("companion.services.audio.asyncio.create_subprocess_exec", side_effect=fake_exec):
        with patch("companion.services.audio.asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await svc.run()

    # Delays should double: 10, 20, 40 ...
    assert sleep_calls[0] == 10.0
    assert sleep_calls[1] == 20.0
    assert sleep_calls[2] == 40.0


async def test_run_backoff_resets_on_success() -> None:
    """retry_delay resets to base once connection is stable."""
    svc = _service()

    # _is_connected and _connect both call create_subprocess_exec, so interleave:
    # check→connect→check→connect→check(stable)
    responses = [
        _mock_proc(b"Connected: no"),  # _is_connected() #1 → not connected
        _mock_proc(b""),  # _connect() #1
        _mock_proc(b"Connected: no"),  # _is_connected() #2 → not connected
        _mock_proc(b""),  # _connect() #2
        _mock_proc(b"Connected: yes"),  # _is_connected() #3 → stable
    ]

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        return responses.pop(0)

    sleep_calls: list[float] = []
    call_count = 0

    async def fake_sleep(delay: float) -> None:
        nonlocal call_count
        sleep_calls.append(delay)
        call_count += 1
        if call_count >= 3:
            raise asyncio.CancelledError

    with patch("companion.services.audio.asyncio.create_subprocess_exec", side_effect=fake_exec):
        with patch("companion.services.audio.asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await svc.run()

    # 10s, 20s after failures; then _CHECK_INTERVAL (30s) after success
    assert sleep_calls[0] == 10.0
    assert sleep_calls[1] == 20.0
    assert sleep_calls[2] == 30.0


async def test_run_skips_connect_when_already_connected() -> None:
    svc = _service()

    connected_proc = _mock_proc(b"Connected: yes")

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        return connected_proc

    connect_called = False
    original_connect = svc._connect

    async def spy_connect() -> None:
        nonlocal connect_called
        connect_called = True
        await original_connect()

    svc._connect = spy_connect  # type: ignore[method-assign]

    async def fake_sleep(delay: float) -> None:
        raise asyncio.CancelledError

    with patch("companion.services.audio.asyncio.create_subprocess_exec", side_effect=fake_exec):
        with patch("companion.services.audio.asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await svc.run()

    assert not connect_called


# ---------------------------------------------------------------------------
# Clean cancellation
# ---------------------------------------------------------------------------


async def test_run_cancels_cleanly() -> None:
    svc = _service()

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        return _mock_proc(b"Connected: yes")

    async def fake_sleep(delay: float) -> None:
        raise asyncio.CancelledError

    with patch("companion.services.audio.asyncio.create_subprocess_exec", side_effect=fake_exec):
        with patch("companion.services.audio.asyncio.sleep", side_effect=fake_sleep):
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
        return_value=_mock_proc(b"Device AA:BB:CC:DD:EE:FF\n\tConnected: yes\n"),
    ):
        assert await svc._is_connected() is True


async def test_is_connected_false() -> None:
    svc = _service()
    with patch(
        "companion.services.audio.asyncio.create_subprocess_exec",
        return_value=_mock_proc(b"Device AA:BB:CC:DD:EE:FF\n\tConnected: no\n"),
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
        return_value=_mock_proc(),
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
    proc.wait = AsyncMock(side_effect=TimeoutError)
    with patch(
        "companion.services.audio.asyncio.create_subprocess_exec",
        return_value=proc,
    ):
        await svc._connect()  # must not raise


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
    assert queue.get_nowait() == AudioReadyChanged(audio_ready=True)


async def test_set_audio_ready_emits_event_on_true_to_false() -> None:
    svc = _service()
    queue = svc.subscribe()
    queue.get_nowait()  # consume the initial-state event (False)
    svc._set_audio_ready(True)
    queue.get_nowait()  # consume the True transition event

    svc._set_audio_ready(False)  # True → False: event
    assert not queue.empty()
    assert queue.get_nowait() == AudioReadyChanged(audio_ready=False)


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
    assert queue.get_nowait() == AudioReadyChanged(audio_ready=False)

    # Now set ready and subscribe again
    svc._set_audio_ready(True)
    queue2 = svc.subscribe()
    assert queue2.get_nowait() == AudioReadyChanged(audio_ready=True)


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
    assert q1.get_nowait() == AudioReadyChanged(audio_ready=True)
    assert q2.get_nowait() == AudioReadyChanged(audio_ready=True)


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
        _mock_proc(b"Connected: yes"),  # _is_connected() → connected
    ]

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        return responses.pop(0)

    async def fake_sleep(delay: float) -> None:
        raise asyncio.CancelledError

    with patch("companion.services.audio.asyncio.create_subprocess_exec", side_effect=fake_exec):
        with patch("companion.services.audio.asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await svc.run()

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())

    assert AudioReadyChanged(audio_ready=True) in events


async def test_run_emits_audio_ready_false_on_cancel() -> None:
    """audio_ready transitions False when run() is cancelled while connected."""
    svc = _service()

    call_count = 0

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        return _mock_proc(b"Connected: yes")

    async def fake_sleep(delay: float) -> None:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise asyncio.CancelledError

    with patch("companion.services.audio.asyncio.create_subprocess_exec", side_effect=fake_exec):
        with patch("companion.services.audio.asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await svc.run()

    assert svc.audio_ready is False
