"""Tests for services REST endpoints — Spotify status, restart, and debug bundle."""

from __future__ import annotations

import asyncio
import json
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock

from companion.config import SpotifySettings
from companion.config_store import ConfigStore, PortalConfig
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


def _make_client(status: SpotifyStatus, tmp_path: Path | None = None) -> AsyncClient:
    spotify = MagicMock()
    type(spotify).status = PropertyMock(return_value=status)
    spotify.settings = SpotifySettings(connect_name=status.device_name, bitrate=320)
    spotify.update_settings = MagicMock()

    if tmp_path is None:
        import tempfile

        _dir = tempfile.mkdtemp()
        cfg_path = Path(_dir) / "config.json"
    else:
        cfg_path = tmp_path / "config.json"

    store = ConfigStore(cfg_path)

    app = create_daemon_app(_make_manager(), DaemonSettings())
    app.include_router(make_services_router(spotify, store))
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
    spotify.settings = SpotifySettings()
    spotify.update_settings = MagicMock()

    import tempfile

    store = ConfigStore(Path(tempfile.mkdtemp()) / "config.json")

    app = create_daemon_app(_make_manager(), settings)
    app.include_router(make_services_router(spotify, store))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/v1/spotify")
    assert r.status_code == 200


async def test_spotify_response_shape() -> None:
    """Response must contain exactly running, active, device_name."""
    async with _make_client(_READY) as client:
        r = await client.get("/api/v1/spotify")
    body = r.json()
    assert set(body.keys()) == {"running", "active", "device_name"}


# ---------------------------------------------------------------------------
# POST /api/v1/spotify/restart
# ---------------------------------------------------------------------------


async def test_spotify_restart_returns_204(tmp_path: Path) -> None:
    async with _make_client(_READY, tmp_path) as client:
        r = await client.post("/api/v1/spotify/restart")
    assert r.status_code == 204


async def test_spotify_restart_calls_update_settings(tmp_path: Path) -> None:
    """Restart reads the config file and calls update_settings on the service."""
    cfg_path = tmp_path / "config.json"
    store = ConfigStore(cfg_path)
    store.write(PortalConfig(spotify_connect_name="Living Room", spotify_bitrate=160))

    spotify = MagicMock()
    type(spotify).status = PropertyMock(return_value=_READY)
    spotify.settings = SpotifySettings(connect_name="PartyBox", bitrate=320)
    spotify.update_settings = MagicMock()

    app = create_daemon_app(_make_manager(), DaemonSettings())
    app.include_router(make_services_router(spotify, store))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/v1/spotify/restart")

    assert r.status_code == 204
    spotify.update_settings.assert_called_once()
    new_settings = spotify.update_settings.call_args[0][0]
    assert new_settings.connect_name == "Living Room"
    assert new_settings.bitrate == 160


async def test_spotify_restart_is_public() -> None:
    """POST /api/v1/spotify/restart must not require an API key."""
    from partyboxd.config import ApiSettings

    settings = DaemonSettings(api=ApiSettings(api_key="secret"))
    spotify = MagicMock()
    type(spotify).status = PropertyMock(return_value=_READY)
    spotify.settings = SpotifySettings()
    spotify.update_settings = MagicMock()

    import tempfile

    store = ConfigStore(Path(tempfile.mkdtemp()) / "config.json")

    app = create_daemon_app(_make_manager(), settings)
    app.include_router(make_services_router(spotify, store))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/v1/spotify/restart")
    assert r.status_code == 204


# ---------------------------------------------------------------------------
# GET /api/v1/debug/bundle
# ---------------------------------------------------------------------------


async def test_debug_bundle_returns_200(tmp_path: Path) -> None:
    async with _make_client(_READY, tmp_path) as client:
        r = await client.get("/api/v1/debug/bundle")
    assert r.status_code == 200


async def test_debug_bundle_is_zip(tmp_path: Path) -> None:
    async with _make_client(_READY, tmp_path) as client:
        r = await client.get("/api/v1/debug/bundle")
    assert r.headers["content-type"] == "application/zip"
    assert zipfile.is_zipfile(BytesIO(r.content))


async def test_debug_bundle_contains_expected_files(tmp_path: Path) -> None:
    async with _make_client(_READY, tmp_path) as client:
        r = await client.get("/api/v1/debug/bundle")
    with zipfile.ZipFile(BytesIO(r.content)) as zf:
        names = set(zf.namelist())
    assert {
        "version.json",
        "config.json",
        "services.json",
        "system.json",
        "device.json",
    } <= names


async def test_debug_bundle_version_json_has_required_fields(tmp_path: Path) -> None:
    async with _make_client(_READY, tmp_path) as client:
        r = await client.get("/api/v1/debug/bundle")
    with zipfile.ZipFile(BytesIO(r.content)) as zf:
        version = json.loads(zf.read("version.json"))
    assert "partyboxd" in version
    assert "python" in version
    assert "generated_at" in version


async def test_debug_bundle_services_json_has_spotify(tmp_path: Path) -> None:
    async with _make_client(_PLAYING, tmp_path) as client:
        r = await client.get("/api/v1/debug/bundle")
    with zipfile.ZipFile(BytesIO(r.content)) as zf:
        services = json.loads(zf.read("services.json"))
    assert services["spotify"]["running"] is True
    assert services["spotify"]["active"] is True


async def test_debug_bundle_services_json_has_audio(tmp_path: Path) -> None:
    async with _make_client(_READY, tmp_path) as client:
        r = await client.get("/api/v1/debug/bundle")
    with zipfile.ZipFile(BytesIO(r.content)) as zf:
        services = json.loads(zf.read("services.json"))
    assert "audio" in services


async def test_debug_bundle_device_json_captures_battery_status(tmp_path: Path) -> None:
    from partybox.protocol.messages import BatteryStatusResponse, ChargingStatus

    spotify = MagicMock()
    type(spotify).status = PropertyMock(return_value=_READY)
    store = ConfigStore(tmp_path / "config.json")

    manager = _make_manager()
    type(manager).snapshot = PropertyMock(
        return_value=StatusSnapshot(
            connected=True,
            address="AA:BB:CC:DD:EE:FF",
            firmware="26.2.10",
            battery=100,
            battery_status=BatteryStatusResponse(
                remaining_capacity_mah=4755,
                full_charge_capacity_mah=4755,
                charging_status=ChargingStatus.FULL,
            ),
        )
    )

    app = create_daemon_app(manager, DaemonSettings())
    app.include_router(make_services_router(spotify, store, manager=manager))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/v1/debug/bundle")
    with zipfile.ZipFile(BytesIO(r.content)) as zf:
        device = json.loads(zf.read("device.json"))
    assert device["available"] is True
    assert device["connected"] is True
    assert device["battery_status"]["charging_status"] == "full"
    assert device["battery_status"]["charge_percent"] == 100


async def test_debug_bundle_has_content_disposition(tmp_path: Path) -> None:
    async with _make_client(_READY, tmp_path) as client:
        r = await client.get("/api/v1/debug/bundle")
    assert "attachment" in r.headers.get("content-disposition", "")
    assert ".zip" in r.headers.get("content-disposition", "")
