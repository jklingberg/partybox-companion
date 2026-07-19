"""Unit tests for SpotifyService.

No librespot binary is required. Tests cover:
- Initial state
- Command construction
- Playback state from librespot's --onevent hook (via the Unix event socket)
- Graceful handling when librespot is not installed
- Clean cancellation (shutdown)
"""

from __future__ import annotations

import asyncio
import stat
import tempfile
from pathlib import Path
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
    runtime_dir: Path | None = None,
) -> SpotifyService:
    return SpotifyService(
        SpotifySettings(connect_name=connect_name, bitrate=bitrate, backend=backend),  # type: ignore[arg-type]
        runtime_dir=runtime_dir if runtime_dir is not None else Path(tempfile.mkdtemp()),
    )


# ---------------------------------------------------------------------------
# SpotifyStatus dataclass
# ---------------------------------------------------------------------------


def test_spotify_status_fields() -> None:
    s = SpotifyStatus(running=True, state="stopped", device_name="Test")
    assert s.running is True
    assert s.state == "stopped"
    assert s.device_name == "Test"


# ---------------------------------------------------------------------------
# Initial service state
# ---------------------------------------------------------------------------


def test_initial_status_not_running() -> None:
    svc = _service()
    assert svc.status.running is False
    assert svc.status.state == "stopped"


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


def test_build_command_includes_onevent_hook() -> None:
    svc = _service()
    cmd = svc._build_command()
    assert "--onevent" in cmd
    idx = cmd.index("--onevent")
    assert cmd[idx + 1] == str(svc._onevent_script_path)


def test_build_command_includes_emit_sink_events() -> None:
    svc = _service()
    assert "--emit-sink-events" in svc._build_command()


# ---------------------------------------------------------------------------
# Playback state from librespot's --onevent hook
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ("playing", "playing"),
        ("paused", "paused"),
        ("stopped", "stopped"),
        ("session_disconnected", "stopped"),
    ],
)
def test_apply_player_event_sets_state(event: str, expected: str) -> None:
    svc = _service()
    svc._apply_player_event(event)
    assert svc._state == expected


@pytest.mark.parametrize("event", ["changed", "loading", "preloading", "seeked", "volume_set", ""])
def test_apply_player_event_ignores_unrecognized(event: str) -> None:
    svc = _service()
    svc._state = "playing"
    svc._apply_player_event(event)
    assert svc._state == "playing"  # unchanged


def test_apply_player_event_playing_then_paused() -> None:
    """The bug this replaces: paused must be distinguishable from playing."""
    svc = _service()
    svc._apply_player_event("playing")
    assert svc._state == "playing"
    svc._apply_player_event("paused")
    assert svc._state == "paused"


# ---------------------------------------------------------------------------
# --onevent launcher script + event socket
# ---------------------------------------------------------------------------


def test_ensure_runtime_files_writes_executable_script(tmp_path: Path) -> None:
    svc = _service(runtime_dir=tmp_path)
    svc._ensure_runtime_files()
    script = svc._onevent_script_path
    assert script.exists()
    assert script.stat().st_mode & 0o111  # executable by someone
    assert "companion.services._librespot_onevent" in script.read_text()


def test_ensure_runtime_files_sets_runtime_dir_permissions(tmp_path: Path) -> None:
    svc = _service(runtime_dir=tmp_path)
    svc._ensure_runtime_files()
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700


def test_ensure_runtime_files_leaves_no_tmp_file_behind(tmp_path: Path) -> None:
    svc = _service(runtime_dir=tmp_path)
    svc._ensure_runtime_files()
    assert list(tmp_path.glob("*.tmp")) == []


async def test_start_event_server_retries_on_oserror(tmp_path: Path) -> None:
    """A transient OSError while setting up the socket is retried, not raised."""
    svc = _service(runtime_dir=tmp_path)
    real_ensure = svc._ensure_runtime_files
    calls = {"n": 0}

    def flaky_ensure() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("runtime dir not ready yet")
        real_ensure()

    svc._ensure_runtime_files = flaky_ensure  # type: ignore[method-assign]

    with patch("companion.services.spotify.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        server = await svc._start_event_server()

    assert calls["n"] == 2
    mock_sleep.assert_called_once()
    server.close()
    await server.wait_closed()


async def test_event_socket_round_trip(tmp_path: Path) -> None:
    """A client connecting to the event socket and sending PLAYER_EVENT updates state."""
    svc = _service(runtime_dir=tmp_path)
    svc._ensure_runtime_files()
    server = await asyncio.start_unix_server(
        svc._handle_event_connection, path=str(svc._event_sock_path)
    )
    try:
        _reader, writer = await asyncio.open_unix_connection(str(svc._event_sock_path))
        writer.write(b"playing\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

        for _ in range(50):
            if svc._state == "playing":
                break
            await asyncio.sleep(0.01)
        assert svc._state == "playing"
    finally:
        server.close()
        await server.wait_closed()


# ---------------------------------------------------------------------------
# subscribe / unsubscribe
# ---------------------------------------------------------------------------


def test_subscribe_delivers_current_state_immediately() -> None:
    svc = _service(connect_name="Living Room")
    queue = svc.subscribe()
    assert not queue.empty()
    assert queue.get_nowait() == SpotifyStatusChanged(
        running=False, state="stopped", device_name="Living Room"
    )


def test_subscribe_returns_queue_that_receives_events() -> None:
    svc = _service()
    queue = svc.subscribe()
    queue.get_nowait()  # drain initial-state event

    svc._apply_player_event("playing")
    assert not queue.empty()
    event = queue.get_nowait()
    assert event.running is False
    assert event.state == "playing"


def test_no_event_emitted_when_status_unchanged() -> None:
    svc = _service()
    queue = svc.subscribe()
    queue.get_nowait()  # drain initial-state event

    svc._apply_player_event("stopped")  # already stopped — no-op
    assert queue.empty()


def test_unsubscribe_stops_delivery() -> None:
    svc = _service()
    queue = svc.subscribe()
    queue.get_nowait()  # drain initial-state event
    svc.unsubscribe(queue)

    svc._apply_player_event("playing")
    assert queue.empty()


# ---------------------------------------------------------------------------
# Graceful degradation when librespot is not installed
# ---------------------------------------------------------------------------


async def test_run_sets_not_running_when_librespot_missing(tmp_path: Path) -> None:
    """When librespot is not in PATH the service stays not-running."""
    svc = _service(runtime_dir=tmp_path)

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


async def test_run_cancels_cleanly_without_librespot(tmp_path: Path) -> None:
    """Cancelling the task raises CancelledError and leaves running=False."""
    svc = _service(runtime_dir=tmp_path)

    with patch("companion.services.spotify.shutil.which", return_value=None):
        with patch("companion.services.spotify.asyncio.sleep", new_callable=AsyncMock):
            task = asyncio.create_task(svc.run())
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert svc.status.running is False
    assert svc.status.state == "stopped"


async def test_terminate_is_noop_when_no_process() -> None:
    """_terminate() must not raise when no subprocess is running."""
    svc = _service()
    await svc._terminate()  # should complete without error


# ---------------------------------------------------------------------------
# Full lifecycle: run() -> playing -> paused -> cancel -> socket cleaned up
# ---------------------------------------------------------------------------


async def test_full_lifecycle_playing_paused_cancel(tmp_path: Path) -> None:
    """End-to-end: the event socket a real onevent hook would talk to.

    librespot itself is unavailable in this environment, but _start_event_server()
    binds the socket unconditionally before entering the librespot retry loop, so
    the socket is live regardless — this exercises the same connection path a real
    librespot --onevent invocation would use.
    """
    svc = _service(runtime_dir=tmp_path)

    async def _send(event: str) -> None:
        _reader, writer = await asyncio.open_unix_connection(str(svc._event_sock_path))
        writer.write(event.encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def _wait_for_state(expected: str) -> None:
        for _ in range(50):
            if svc._state == expected:
                return
            await asyncio.sleep(0.01)
        pytest.fail(f"state never became {expected!r}, last seen {svc._state!r}")

    # asyncio.sleep is deliberately *not* mocked here (unlike the other run()
    # tests): mocking it replaces the real asyncio.sleep globally (spotify.py
    # does `import asyncio`, so `companion.services.spotify.asyncio` is the
    # same module object the test's own `await asyncio.sleep(...)` polling
    # below relies on) — an AsyncMock's awaited call resolves without an
    # actual suspension point, so it never hands control back to the event
    # loop, and the polling below would never observe svc.run() making
    # progress. Real sleep is fine here: librespot being "missing" only
    # means _run_once() parks in a real asyncio.sleep(_NOT_FOUND_RETRY),
    # which task.cancel() below interrupts immediately regardless of how
    # long that sleep is.
    with patch("companion.services.spotify.shutil.which", return_value=None):
        task = asyncio.create_task(svc.run())
        for _ in range(50):
            if svc._event_sock_path.exists():
                break
            await asyncio.sleep(0.01)
        assert svc._event_sock_path.exists()

        await _send("playing")
        await _wait_for_state("playing")

        await _send("paused")
        await _wait_for_state("paused")

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert not svc._event_sock_path.exists()


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
