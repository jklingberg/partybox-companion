"""Tests for GET /api/v1/volume and POST /api/v1/volume."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest
from companion.config import SpotifySettings
from companion.services.router import make_services_router
from companion.services.spotify import SpotifyStatus
from companion.volume import VolumeState
from httpx import ASGITransport, AsyncClient
from partyboxd.api import create_app as create_daemon_app
from partyboxd.config import Settings as DaemonSettings
from partyboxd.device.manager import DeviceNotConnectedError, StatusSnapshot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager(
    *,
    connected: bool = True,
    get_volume_result: int | None = None,
    get_volume_exc: Exception | None = None,
    set_volume_exc: Exception | None = None,
) -> MagicMock:
    manager = MagicMock()
    snap = StatusSnapshot(
        connected=connected,
        address="AA:BB:CC:DD:EE:FF" if connected else None,
        firmware=None,
        battery=None,
    )
    type(manager).snapshot = PropertyMock(return_value=snap)
    if get_volume_exc is not None:
        manager.get_volume = AsyncMock(side_effect=get_volume_exc)
    else:
        manager.get_volume = AsyncMock(return_value=get_volume_result)
    if set_volume_exc is not None:
        manager.set_volume = AsyncMock(side_effect=set_volume_exc)
    else:
        manager.set_volume = AsyncMock()
    return manager


def _make_spotify() -> MagicMock:
    spotify = MagicMock()
    type(spotify).status = PropertyMock(
        return_value=SpotifyStatus(running=False, state="stopped", device_name="PartyBox")
    )
    spotify.settings = SpotifySettings()
    spotify.update_settings = MagicMock()
    return spotify


def _make_client(
    manager: MagicMock | None = None,
    volume_state: VolumeState | None = None,
) -> AsyncClient:
    import tempfile
    from pathlib import Path

    store_path = Path(tempfile.mkdtemp()) / "config.json"
    from companion.config_store import ConfigStore

    store = ConfigStore(store_path)
    app = create_daemon_app(manager or _make_manager(), DaemonSettings())
    app.include_router(
        make_services_router(_make_spotify(), store, manager=manager, volume_state=volume_state)
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# GET /api/v1/volume
# ---------------------------------------------------------------------------


async def test_get_volume_returns_hardware_level_when_available() -> None:
    manager = _make_manager(get_volume_result=72)
    async with _make_client(manager) as client:
        r = await client.get("/api/v1/volume")
    assert r.status_code == 200
    assert r.json() == {"level": 72, "source": "ble"}


async def test_get_volume_falls_back_to_state_when_ble_returns_none() -> None:
    manager = _make_manager(get_volume_result=None)
    vs = VolumeState()
    vs.update(55, "spotify")
    async with _make_client(manager, vs) as client:
        r = await client.get("/api/v1/volume")
    assert r.status_code == 200
    assert r.json() == {"level": 55, "source": "spotify"}


async def test_get_volume_falls_back_to_state_when_disconnected() -> None:
    manager = _make_manager(get_volume_exc=DeviceNotConnectedError())
    vs = VolumeState()
    vs.update(30, "api")
    async with _make_client(manager, vs) as client:
        r = await client.get("/api/v1/volume")
    assert r.status_code == 200
    assert r.json() == {"level": 30, "source": "api"}


async def test_get_volume_returns_null_when_nothing_known() -> None:
    manager = _make_manager(get_volume_result=None)
    async with _make_client(manager, VolumeState()) as client:
        r = await client.get("/api/v1/volume")
    assert r.status_code == 200
    assert r.json() == {"level": None, "source": None}


async def test_get_volume_no_manager_returns_state() -> None:
    vs = VolumeState()
    vs.update(40, "spotify")
    async with _make_client(None, vs) as client:
        r = await client.get("/api/v1/volume")
    assert r.status_code == 200
    assert r.json() == {"level": 40, "source": "spotify"}


async def test_get_volume_no_manager_no_state_returns_null() -> None:
    async with _make_client(None, None) as client:
        r = await client.get("/api/v1/volume")
    assert r.status_code == 200
    assert r.json() == {"level": None, "source": None}


# ---------------------------------------------------------------------------
# POST /api/v1/volume
# ---------------------------------------------------------------------------


async def test_post_volume_returns_204() -> None:
    manager = _make_manager()
    async with _make_client(manager, VolumeState()) as client:
        r = await client.post("/api/v1/volume", json={"level": 50})
    assert r.status_code == 204


async def test_post_volume_calls_manager_set_volume() -> None:
    manager = _make_manager()
    vs = VolumeState()
    async with _make_client(manager, vs) as client:
        await client.post("/api/v1/volume", json={"level": 80})
    manager.set_volume.assert_awaited_once_with(80)


async def test_post_volume_updates_state() -> None:
    manager = _make_manager()
    vs = VolumeState()
    async with _make_client(manager, vs) as client:
        await client.post("/api/v1/volume", json={"level": 65})
    assert vs.level == 65
    assert vs.source == "api"


async def test_post_volume_updates_state_when_disconnected() -> None:
    manager = _make_manager(set_volume_exc=DeviceNotConnectedError())
    vs = VolumeState()
    async with _make_client(manager, vs) as client:
        r = await client.post("/api/v1/volume", json={"level": 42})
    assert r.status_code == 204
    assert vs.level == 42
    assert vs.source == "api"


async def test_post_volume_updates_state_when_not_implemented() -> None:
    manager = _make_manager(set_volume_exc=NotImplementedError())
    vs = VolumeState()
    async with _make_client(manager, vs) as client:
        r = await client.post("/api/v1/volume", json={"level": 10})
    assert r.status_code == 204
    assert vs.level == 10
    assert vs.source == "api"


async def test_post_volume_invalid_level_returns_422() -> None:
    async with _make_client(None, VolumeState()) as client:
        r = await client.post("/api/v1/volume", json={"level": 101})
    assert r.status_code == 422


async def test_post_volume_negative_level_returns_422() -> None:
    async with _make_client(None, VolumeState()) as client:
        r = await client.post("/api/v1/volume", json={"level": -1})
    assert r.status_code == 422


@pytest.mark.parametrize("level", [0, 100])
async def test_post_volume_boundary_values_accepted(level: int) -> None:
    vs = VolumeState()
    async with _make_client(None, vs) as client:
        r = await client.post("/api/v1/volume", json={"level": level})
    assert r.status_code == 204
    assert vs.level == level
    assert vs.source == "api"
