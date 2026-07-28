"""Unit tests for _gate_spotify_on_audio in companion.__main__.

The gate holds Spotify Connect until the appliance can actually produce audio,
then starts it; stops it after a grace period if audio goes away for long enough
to indicate the speaker is no longer reachable.

No Bluetooth hardware, bluetoothctl, or librespot binary required.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from unittest.mock import MagicMock, patch

import pytest
from companion.__main__ import _gate_spotify_on_audio
from companion.services.audio import AudioReadyChanged, AudioService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _queue(*events: bool) -> asyncio.Queue[AudioReadyChanged]:
    """Pre-populate an audio event queue with the given ready states."""
    q: asyncio.Queue[AudioReadyChanged] = asyncio.Queue()
    for r in events:
        q.put_nowait(AudioReadyChanged(audio_ready=r))
    return q


def _audio_mock(q: asyncio.Queue[AudioReadyChanged]) -> MagicMock:
    m = MagicMock(spec=AudioService)
    m.subscribe.return_value = q
    return m


class _FakeSpotify:
    """Minimal SpotifyService stand-in that tracks lifecycle events."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.run_count = 0

    async def run(self) -> None:
        self.run_count += 1
        self.started.set()
        try:
            await asyncio.sleep(1e9)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class _CrashingSpotify:
    """SpotifyService stand-in whose run() raises shortly after starting."""

    def __init__(self, exc: BaseException | None = None) -> None:
        self.started = asyncio.Event()
        self.run_count = 0
        self._exc = exc if exc is not None else RuntimeError("librespot died")

    async def run(self) -> None:
        self.run_count += 1
        self.started.set()
        await asyncio.sleep(0)
        raise self._exc


class _ReturningSpotify:
    """SpotifyService stand-in whose run() returns cleanly (a violated invariant)."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.run_count = 0

    async def run(self) -> None:
        self.run_count += 1
        self.started.set()
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Gate does not start Spotify until audio is ready
# ---------------------------------------------------------------------------


async def test_does_not_start_spotify_when_audio_not_ready() -> None:
    q = _queue(False)
    spotify = _FakeSpotify()

    task = asyncio.create_task(_gate_spotify_on_audio(_audio_mock(q), spotify))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert not spotify.started.is_set()
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def test_starts_spotify_immediately_when_audio_already_ready() -> None:
    """BehaviorSubject delivers True on subscribe; gate starts without waiting."""
    q = _queue(True)
    spotify = _FakeSpotify()

    task = asyncio.create_task(_gate_spotify_on_audio(_audio_mock(q), spotify))
    await asyncio.sleep(0)  # gate runs, dequeues True, creates spotify_task
    await asyncio.sleep(0)  # spotify_task runs, sets started

    assert spotify.started.is_set()
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def test_starts_spotify_on_audio_ready_transition() -> None:
    q = _queue(False)
    spotify = _FakeSpotify()

    task = asyncio.create_task(_gate_spotify_on_audio(_audio_mock(q), spotify))
    await asyncio.sleep(0)  # gate runs, dequeues False, suspends on queue.get()
    assert not spotify.started.is_set()

    q.put_nowait(AudioReadyChanged(audio_ready=True))
    await asyncio.sleep(0)  # gate wakes, dequeues True, creates spotify_task
    await asyncio.sleep(0)  # spotify_task runs, sets started

    assert spotify.started.is_set()
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def test_does_not_start_spotify_twice_on_duplicate_ready() -> None:
    """Gate creates at most one Spotify task regardless of repeated True events."""
    q = _queue(True, True)
    spotify = _FakeSpotify()

    task = asyncio.create_task(_gate_spotify_on_audio(_audio_mock(q), spotify))
    await asyncio.sleep(0)  # gate: dequeue first True, create spotify_task
    await asyncio.sleep(0)  # spotify_task: set started; gate: dequeue second True (no-op)
    await asyncio.sleep(0)  # extra yield

    assert spotify.run_count == 1
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# Grace period
# ---------------------------------------------------------------------------


@patch("companion.__main__._AUDIO_GRACE_SECONDS", 0.02)
async def test_stops_spotify_after_grace_expires() -> None:
    q = _queue(True)
    spotify = _FakeSpotify()
    audio = _audio_mock(q)

    task = asyncio.create_task(_gate_spotify_on_audio(audio, spotify))
    await asyncio.sleep(0)  # gate: dequeue True, create spotify_task
    await asyncio.sleep(0)  # spotify_task: set started
    assert spotify.started.is_set()

    q.put_nowait(AudioReadyChanged(audio_ready=False))
    await asyncio.sleep(0.1)  # well past 0.02 s grace

    assert spotify.cancelled.is_set()
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


@patch("companion.__main__._AUDIO_GRACE_SECONDS", 0.1)
async def test_keeps_spotify_when_audio_recovers_within_grace() -> None:
    q = _queue(True)
    spotify = _FakeSpotify()

    task = asyncio.create_task(_gate_spotify_on_audio(_audio_mock(q), spotify))
    await asyncio.sleep(0)  # gate: dequeue True, create spotify_task
    await asyncio.sleep(0)  # spotify_task: set started
    assert spotify.started.is_set()

    q.put_nowait(AudioReadyChanged(audio_ready=False))
    await asyncio.sleep(0)
    q.put_nowait(AudioReadyChanged(audio_ready=True))
    await asyncio.sleep(0.01)  # well within 0.1 s grace

    assert not spotify.cancelled.is_set()
    assert spotify.run_count == 1
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


@patch("companion.__main__._AUDIO_GRACE_SECONDS", 0.02)
async def test_restarts_spotify_after_stop_and_recovery() -> None:
    """Gate restarts Spotify if audio returns after a full stop."""
    q = _queue(True)
    spotify = _FakeSpotify()

    task = asyncio.create_task(_gate_spotify_on_audio(_audio_mock(q), spotify))
    await asyncio.sleep(0)  # gate: dequeue True, create spotify_task
    await asyncio.sleep(0)  # spotify_task: set started
    assert spotify.run_count == 1

    # Audio goes away; grace expires; Spotify stops.
    q.put_nowait(AudioReadyChanged(audio_ready=False))
    await asyncio.sleep(0.1)
    assert spotify.cancelled.is_set()

    # Audio returns; gate should start a fresh Spotify task.
    q.put_nowait(AudioReadyChanged(audio_ready=True))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert spotify.run_count == 2
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# Cancellation / cleanup
# ---------------------------------------------------------------------------


async def test_cancels_spotify_when_gate_is_cancelled() -> None:
    q = _queue(True)
    spotify = _FakeSpotify()

    task = asyncio.create_task(_gate_spotify_on_audio(_audio_mock(q), spotify))
    await asyncio.sleep(0)  # gate: dequeue True, create spotify_task
    await asyncio.sleep(0)  # spotify_task: set started
    assert spotify.started.is_set()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert spotify.cancelled.is_set()


async def test_cancels_cleanly_when_spotify_never_started() -> None:
    q = _queue(False)
    audio = _audio_mock(q)
    spotify = _FakeSpotify()

    task = asyncio.create_task(_gate_spotify_on_audio(audio, spotify))
    await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not spotify.started.is_set()
    audio.unsubscribe.assert_called_once_with(q)


async def test_unsubscribes_on_cancellation() -> None:
    q = _queue(True)
    audio = _audio_mock(q)
    spotify = _FakeSpotify()

    task = asyncio.create_task(_gate_spotify_on_audio(audio, spotify))
    await asyncio.sleep(0)  # gate: dequeue True, create spotify_task
    await asyncio.sleep(0)  # spotify_task: set started

    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    audio.unsubscribe.assert_called_once_with(q)


# ---------------------------------------------------------------------------
# spotify_task supervision (issue #65)
# ---------------------------------------------------------------------------


async def test_propagates_spotify_exception_while_audio_ready() -> None:
    """An exception from spotify.run() must propagate even while audio stays ready.

    This is the exact gap from issue #65: previously spotify_task was only
    awaited on the grace-timeout and cancellation paths, so a crash on the
    common "audio steady" path went unobserved and the Supervisor never
    restarted anything.
    """
    q = _queue(True)
    spotify = _CrashingSpotify(RuntimeError("librespot died"))

    task = asyncio.create_task(_gate_spotify_on_audio(_audio_mock(q), spotify))

    with pytest.raises(RuntimeError, match="librespot died"):
        await asyncio.wait_for(task, timeout=1.0)

    assert spotify.run_count == 1


async def test_propagates_spotify_clean_return_while_audio_ready() -> None:
    """A bare return from spotify.run() is also a violated invariant and must propagate."""
    q = _queue(True)
    spotify = _ReturningSpotify()

    task = asyncio.create_task(_gate_spotify_on_audio(_audio_mock(q), spotify))

    with pytest.raises(RuntimeError, match="exited unexpectedly"):
        await asyncio.wait_for(task, timeout=1.0)


@patch("companion.__main__._AUDIO_GRACE_SECONDS", 10.0)
async def test_propagates_spotify_exception_during_grace_period() -> None:
    """A crash while audio is briefly unavailable (mid grace-period wait) must also propagate."""
    q = _queue(True)
    spotify = _CrashingSpotify(RuntimeError("librespot died mid-grace"))
    audio = _audio_mock(q)

    task = asyncio.create_task(_gate_spotify_on_audio(audio, spotify))
    await asyncio.sleep(0)  # gate: dequeue True, create spotify_task
    await spotify.started.wait()

    q.put_nowait(AudioReadyChanged(audio_ready=False))  # enter grace-period wait

    with pytest.raises(RuntimeError, match="librespot died mid-grace"):
        await asyncio.wait_for(task, timeout=1.0)


async def test_unsubscribes_when_spotify_crashes() -> None:
    """The finally block's cleanup must run (and not double-await the failed task)."""
    q = _queue(True)
    spotify = _CrashingSpotify(RuntimeError("boom"))
    audio = _audio_mock(q)

    task = asyncio.create_task(_gate_spotify_on_audio(audio, spotify))

    with pytest.raises(RuntimeError, match="boom"):
        await asyncio.wait_for(task, timeout=1.0)

    audio.unsubscribe.assert_called_once_with(q)


@patch("companion.__main__._AUDIO_GRACE_SECONDS", 0.02)
async def test_unsubscribes_after_grace_expiry() -> None:
    """unsubscribe() is called whether Spotify is stopped via grace or cancellation."""
    q = _queue(True)
    audio = _audio_mock(q)
    spotify = _FakeSpotify()

    task = asyncio.create_task(_gate_spotify_on_audio(audio, spotify))
    await asyncio.sleep(0)  # gate: dequeue True, create spotify_task
    await asyncio.sleep(0)  # spotify_task: set started

    q.put_nowait(AudioReadyChanged(audio_ready=False))
    await asyncio.sleep(0.1)  # grace expires

    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    audio.unsubscribe.assert_called_once_with(q)
