"""Unit tests for the librespot --onevent hook (_librespot_onevent.py).

Runs the hook's main() in-process against a real Unix socket server, rather
than invoking it as a subprocess — it's pure stdlib (os/socket/logging), so
there's nothing to gain from the extra subprocess overhead.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from companion.services import _librespot_onevent


def test_main_noop_without_socket_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COMPANION_SPOTIFY_EVENT_SOCK", raising=False)
    monkeypatch.setenv("PLAYER_EVENT", "playing")
    assert _librespot_onevent.main() == 0


def test_main_noop_without_player_event(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("COMPANION_SPOTIFY_EVENT_SOCK", str(tmp_path))
    monkeypatch.delenv("PLAYER_EVENT", raising=False)
    assert _librespot_onevent.main() == 0


async def test_main_forwards_event_to_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sock_path = tmp_path / "events.sock"
    received: list[str] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        received.append(line.decode().strip())
        writer.close()

    server = await asyncio.start_unix_server(handle, path=str(sock_path))
    try:
        monkeypatch.setenv("COMPANION_SPOTIFY_EVENT_SOCK", str(sock_path))
        monkeypatch.setenv("PLAYER_EVENT", "paused")
        # main() is synchronous (blocking socket calls); running it directly is
        # fine here since the connect/send complete near-instantly against a
        # local socket that's already listening.
        result = _librespot_onevent.main()
        assert result == 0

        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.01)
        assert received == ["paused"]
    finally:
        server.close()
        await server.wait_closed()


def test_main_logs_debug_on_connection_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No listener on the socket path — main() must not raise and should return 0."""
    monkeypatch.setenv("COMPANION_SPOTIFY_EVENT_SOCK", str(tmp_path / "no-such.sock"))
    monkeypatch.setenv("PLAYER_EVENT", "playing")

    with caplog.at_level(logging.DEBUG, logger="companion.services._librespot_onevent"):
        result = _librespot_onevent.main()

    assert result == 0
    assert "failed to forward" in caplog.text
    assert all(record.levelno == logging.DEBUG for record in caplog.records)
