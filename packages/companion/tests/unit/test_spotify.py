"""Unit tests for SpotifyService.

No librespot binary is required. Tests cover:
- Initial state
- Command construction
- Playback-state inference from log lines
- Graceful handling when librespot is not installed
- Clean cancellation (shutdown)
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from companion.config import SpotifySettings
from companion.services.spotify import SpotifyService, SpotifyStatus, SpotifyStatusChanged

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _service(
    connect_name: str = "PartyBox",
    bitrate: int = 320,
    backend: str | None = None,
) -> SpotifyService:
    return SpotifyService(
        SpotifySettings(connect_name=connect_name, bitrate=bitrate, backend=backend)  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# SpotifyStatus dataclass
# ---------------------------------------------------------------------------


def test_spotify_status_fields() -> None:
    s = SpotifyStatus(running=True, active=False, device_name="Test")
    assert s.running is True
    assert s.active is False
    assert s.device_name == "Test"


# ---------------------------------------------------------------------------
# Initial service state
# ---------------------------------------------------------------------------


def test_initial_status_not_running() -> None:
    svc = _service()
    assert svc.status.running is False
    assert svc.status.active is False


def test_initial_status_device_name() -> None:
    svc = _service(connect_name="Living Room")
    assert svc.status.device_name == "Living Room"


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


def test_build_command_contains_librespot() -> None:
    svc = _service()
    cmd = svc._build_command()
    assert cmd[0] == "librespot"


def test_build_command_includes_name() -> None:
    svc = _service(connect_name="My Speaker")
    cmd = svc._build_command()
    assert "--name" in cmd
    idx = cmd.index("--name")
    assert cmd[idx + 1] == "My Speaker"


def test_build_command_includes_bitrate() -> None:
    svc = _service(bitrate=160)
    cmd = svc._build_command()
    assert "--bitrate" in cmd
    idx = cmd.index("--bitrate")
    assert cmd[idx + 1] == "160"


def test_build_command_no_backend_by_default() -> None:
    svc = _service()
    assert "--backend" not in svc._build_command()


def test_build_command_includes_backend_when_set() -> None:
    svc = _service(backend="pulseaudio")
    cmd = svc._build_command()
    assert "--backend" in cmd
    idx = cmd.index("--backend")
    assert cmd[idx + 1] == "pulseaudio"


def test_build_command_includes_disable_audio_cache() -> None:
    svc = _service()
    assert "--disable-audio-cache" in svc._build_command()


# ---------------------------------------------------------------------------
# Playback-state inference from log lines
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "Track ABC is now playing.",
        "Loading track spotify:track:XYZ",
        "Preloading next track",
        "INFO librespot_playback: track IS NOW PLAYING",  # case-insensitive
    ],
)
def test_infer_active_from_playing_lines(line: str) -> None:
    svc = _service()
    svc._infer_playback_state(line)
    assert svc._active is True


@pytest.mark.parametrize(
    "line",
    [
        "Track paused",
        "Track stopped",
        "End of track",
        "INFO librespot_playback: TRACK STOPPED",  # case-insensitive
    ],
)
def test_infer_inactive_from_stop_lines(line: str) -> None:
    svc = _service()
    svc._active = True  # seed active state
    svc._infer_playback_state(line)
    assert svc._active is False


def test_infer_no_change_on_irrelevant_line() -> None:
    svc = _service()
    svc._infer_playback_state("Connecting to Spotify")
    assert svc._active is False


def test_infer_idempotent_when_already_active() -> None:
    svc = _service()
    svc._active = True
    # Calling again with an "active" line should not flip state
    svc._infer_playback_state("is now playing")
    assert svc._active is True


def test_infer_idempotent_when_already_inactive() -> None:
    svc = _service()
    svc._infer_playback_state("track stopped")
    assert svc._active is False


# ---------------------------------------------------------------------------
# subscribe / unsubscribe
# ---------------------------------------------------------------------------


def test_subscribe_delivers_current_state_immediately() -> None:
    svc = _service(connect_name="Living Room")
    queue = svc.subscribe()
    assert not queue.empty()
    assert queue.get_nowait() == SpotifyStatusChanged(
        running=False, active=False, device_name="Living Room"
    )


def test_subscribe_returns_queue_that_receives_events() -> None:
    svc = _service()
    queue = svc.subscribe()
    queue.get_nowait()  # drain initial-state event

    svc._infer_playback_state("is now playing")
    assert not queue.empty()
    event = queue.get_nowait()
    assert event.running is False
    assert event.active is True


def test_no_event_emitted_when_status_unchanged() -> None:
    svc = _service()
    queue = svc.subscribe()
    queue.get_nowait()  # drain initial-state event

    svc._infer_playback_state("track stopped")  # already inactive — no-op
    assert queue.empty()


def test_unsubscribe_stops_delivery() -> None:
    svc = _service()
    queue = svc.subscribe()
    queue.get_nowait()  # drain initial-state event
    svc.unsubscribe(queue)

    svc._infer_playback_state("is now playing")
    assert queue.empty()


# ---------------------------------------------------------------------------
# Graceful degradation when librespot is not installed
# ---------------------------------------------------------------------------


async def test_run_sets_not_running_when_librespot_missing() -> None:
    """When librespot is not in PATH the service stays not-running."""
    svc = _service()

    sleep_patch = patch("companion.services.spotify.asyncio.sleep", new_callable=AsyncMock)
    with patch("companion.services.spotify.shutil.which", return_value=None):
        with sleep_patch as mock_sleep:
            task = asyncio.create_task(svc.run())
            # Let run() start and call _run_once() once
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert svc.status.running is False
    # Should have attempted to sleep before retrying
    mock_sleep.assert_called()


# ---------------------------------------------------------------------------
# Clean cancellation
# ---------------------------------------------------------------------------


async def test_run_cancels_cleanly_without_librespot() -> None:
    """Cancelling the task raises CancelledError and leaves running=False."""
    svc = _service()

    with patch("companion.services.spotify.shutil.which", return_value=None):
        with patch("companion.services.spotify.asyncio.sleep", new_callable=AsyncMock):
            task = asyncio.create_task(svc.run())
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert svc.status.running is False
    assert svc.status.active is False


async def test_terminate_is_noop_when_no_process() -> None:
    """_terminate() must not raise when no subprocess is running."""
    svc = _service()
    await svc._terminate()  # should complete without error


# ---------------------------------------------------------------------------
# SpotifySettings validation
# ---------------------------------------------------------------------------


def test_spotify_settings_defaults() -> None:
    s = SpotifySettings()
    assert s.connect_name == "PartyBox"
    assert s.bitrate == 320
    assert s.backend is None


@pytest.mark.parametrize("bitrate", [96, 160, 320])
def test_spotify_settings_valid_bitrates(bitrate: int) -> None:
    s = SpotifySettings(bitrate=bitrate)  # type: ignore[arg-type]
    assert s.bitrate == bitrate
