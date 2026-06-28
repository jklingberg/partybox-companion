"""Tests for GET /api/v1/spotify — Spotify Connect status endpoint."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, PropertyMock

from companion.services.router import make_services_router
from companion.services.spotify import SpotifyStatus
from httpx import ASGITransport, AsyncClient
from partyboxd.api import create_app as create_daemon_app
from partyboxd.config import Settings as DaemonSettings
from partyboxd.device.manager import StatusSnapshot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager() -> MagicMock:
    manager = MagicMock()
    type(manager).snapshot = PropertyMock(
        return_value=StatusSnapshot(connected=False, address=None, firmware=None, battery=None)
    )
    manager.subscribe = MagicMock(return_value=asyncio.Queue())
    manager.unsubscribe = MagicMock()
    return manager


def _make_client(status: SpotifyStatus) -> AsyncClient:
    spotify = MagicMock()
    type(spotify).status = PropertyMock(return_value=status)

    app = create_daemon_app(_make_manager(), DaemonSettings())
    app.include_router(make_services_router(spotify))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


_STOPPED = SpotifyStatus(running=False, active=False, device_name="PartyBox")
_READY = SpotifyStatus(running=True, active=False, device_name="PartyBox")
_PLAYING = SpotifyStatus(running=True, active=True, device_name="Living Room")


# ---------------------------------------------------------------------------
# GET /api/v1/spotify
# ---------------------------------------------------------------------------


async def test_spotify_stopped() -> None:
    async with _make_client(_STOPPED) as client:
        r = await client.get("/api/v1/spotify")
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is False
    assert body["active"] is False
    assert body["device_name"] == "PartyBox"


async def test_spotify_ready() -> None:
    async with _make_client(_READY) as client:
        r = await client.get("/api/v1/spotify")
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is True
    assert body["active"] is False


async def test_spotify_playing() -> None:
    async with _make_client(_PLAYING) as client:
        r = await client.get("/api/v1/spotify")
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is True
    assert body["active"] is True
    assert body["device_name"] == "Living Room"


async def test_spotify_endpoint_is_public() -> None:
    """GET /api/v1/spotify must not require an API key."""
    from partyboxd.config import ApiSettings

    settings = DaemonSettings(api=ApiSettings(api_key="secret"))
    spotify = MagicMock()
    type(spotify).status = PropertyMock(return_value=_READY)

    app = create_daemon_app(_make_manager(), settings)
    app.include_router(make_services_router(spotify))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/v1/spotify")
    assert r.status_code == 200


async def test_spotify_response_shape() -> None:
    """Response must contain exactly running, active, device_name."""
    async with _make_client(_READY) as client:
        r = await client.get("/api/v1/spotify")
    body = r.json()
    assert set(body.keys()) == {"running", "active", "device_name"}
