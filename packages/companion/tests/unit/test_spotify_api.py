"""Tests for services REST endpoints — Spotify status, restart, and debug bundle."""

from __future__ import annotations

import asyncio
import json
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock

from companion.config import SpotifySettings
from companion.config_store import ConfigStore, PortalConfig
from companion.services.router import make_services_router
from companion.services.spotify import SpotifyStatus
from httpx import ASGITransport, AsyncClient
from partyboxd.api import create_app as create_daemon_app
from partyboxd.api.auth import make_auth_dependency
from partyboxd.config import ApiSettings
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


def _make_client(
    status: SpotifyStatus,
    tmp_path: Path | None = None,
    *,
    daemon_settings: DaemonSettings | None = None,
    with_auth: bool = False,
) -> AsyncClient:
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

    settings = daemon_settings or DaemonSettings()
    app = create_daemon_app(_make_manager(), settings)
    app.include_router(
        make_services_router(
            spotify, store, auth=make_auth_dependency(settings) if with_auth else None
        )
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


_STOPPED = SpotifyStatus(running=False, state="stopped", device_name="PartyBox")
_READY = SpotifyStatus(running=True, state="stopped", device_name="PartyBox")
_PLAYING = SpotifyStatus(running=True, state="playing", device_name="Living Room")
_PAUSED = SpotifyStatus(running=True, state="paused", device_name="Living Room")


# ---------------------------------------------------------------------------
# GET /api/v1/spotify
# ---------------------------------------------------------------------------


async def test_spotify_stopped() -> None:
    async with _make_client(_STOPPED) as client:
        r = await client.get("/api/v1/spotify")
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is False
    assert body["state"] == "stopped"
    assert body["device_name"] == "PartyBox"


async def test_spotify_ready() -> None:
    async with _make_client(_READY) as client:
        r = await client.get("/api/v1/spotify")
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is True
    assert body["state"] == "stopped"


async def test_spotify_playing() -> None:
    async with _make_client(_PLAYING) as client:
        r = await client.get("/api/v1/spotify")
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is True
    assert body["state"] == "playing"
    assert body["device_name"] == "Living Room"


async def test_spotify_paused() -> None:
    async with _make_client(_PAUSED) as client:
        r = await client.get("/api/v1/spotify")
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is True
    assert body["state"] == "paused"
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
    """Response must contain exactly running, state, device_name."""
    async with _make_client(_READY) as client:
        r = await client.get("/api/v1/spotify")
    body = r.json()
    assert set(body.keys()) == {"running", "state", "device_name"}


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
    assert services["spotify"]["state"] == "playing"


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


# ---------------------------------------------------------------------------
# POST /api/v1/factory-reset
# ---------------------------------------------------------------------------


def _reset_client(
    tmp_path: Path,
    *,
    sink_address: str | None = None,
    audio: MagicMock | None = None,
    pairing: MagicMock | None = None,
    daemon_settings: DaemonSettings | None = None,
    with_auth: bool = False,
) -> tuple[AsyncClient, MagicMock, ConfigStore]:
    spotify = MagicMock()
    type(spotify).status = PropertyMock(return_value=_READY)
    spotify.settings = SpotifySettings(connect_name="Living Room", bitrate=160, backend="pipewire")
    spotify.update_settings = MagicMock()

    store = ConfigStore(tmp_path / "config.json")
    store.write(
        PortalConfig(
            device_name="Den",
            spotify_connect_name="Living Room",
            spotify_bitrate=160,
            audio_sink_address=sink_address,
        )
    )

    settings = daemon_settings or DaemonSettings()
    app = create_daemon_app(_make_manager(), settings)
    app.include_router(
        make_services_router(
            spotify,
            store,
            audio=audio,
            pairing=pairing,
            auth=make_auth_dependency(settings) if with_auth else None,
        )
    )
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    return client, spotify, store


async def test_factory_reset_returns_204(tmp_path: Path) -> None:
    client, _, _ = _reset_client(tmp_path)
    async with client:
        r = await client.post("/api/v1/factory-reset")
    assert r.status_code == 204


async def test_factory_reset_clears_config(tmp_path: Path) -> None:
    client, _, store = _reset_client(tmp_path, sink_address="50:1B:6A:14:FD:1D")
    async with client:
        r = await client.post("/api/v1/factory-reset")
    assert r.status_code == 204
    assert store.read() == PortalConfig()


async def test_factory_reset_forgets_audio_and_removes_bond(tmp_path: Path) -> None:
    audio = MagicMock()
    pairing = MagicMock()
    pairing.forget = AsyncMock()
    client, _, _ = _reset_client(
        tmp_path, sink_address="50:1B:6A:14:FD:1D", audio=audio, pairing=pairing
    )
    async with client:
        await client.post("/api/v1/factory-reset")
    audio.forget.assert_called_once_with()
    pairing.forget.assert_awaited_once_with("50:1B:6A:14:FD:1D")


async def test_factory_reset_skips_bond_removal_when_no_address(tmp_path: Path) -> None:
    pairing = MagicMock()
    pairing.forget = AsyncMock()
    client, _, _ = _reset_client(tmp_path, sink_address=None, pairing=pairing)
    async with client:
        await client.post("/api/v1/factory-reset")
    pairing.forget.assert_not_awaited()


async def test_factory_reset_restarts_spotify_with_defaults(tmp_path: Path) -> None:
    client, spotify, _ = _reset_client(tmp_path)
    async with client:
        await client.post("/api/v1/factory-reset")
    spotify.update_settings.assert_called_once()
    new = spotify.update_settings.call_args[0][0]
    assert new.connect_name == SpotifySettings().connect_name
    assert new.bitrate == SpotifySettings().bitrate
    assert new.backend == "pipewire"  # env-driven backend preserved, not user config


async def test_factory_reset_resilient_to_bond_removal_failure(tmp_path: Path) -> None:
    audio = MagicMock()
    pairing = MagicMock()
    pairing.forget = AsyncMock(side_effect=RuntimeError("dbus down"))
    client, spotify, store = _reset_client(
        tmp_path, sink_address="50:1B:6A:14:FD:1D", audio=audio, pairing=pairing
    )
    async with client:
        r = await client.post("/api/v1/factory-reset")
    assert r.status_code == 204
    # Config is still wiped even though bond removal raised.
    assert store.read() == PortalConfig()
    spotify.update_settings.assert_called_once()


# ---------------------------------------------------------------------------
# Auth (SEC-02/SEC-04) — factory-reset and debug/bundle respect the API key
# ---------------------------------------------------------------------------


async def test_factory_reset_requires_auth_when_configured(tmp_path: Path) -> None:
    settings = DaemonSettings(api=ApiSettings(api_key="secret"))
    client, _, _ = _reset_client(tmp_path, daemon_settings=settings, with_auth=True)
    async with client:
        r = await client.post("/api/v1/factory-reset")
    assert r.status_code == 401


async def test_factory_reset_accepts_valid_api_key(tmp_path: Path) -> None:
    settings = DaemonSettings(api=ApiSettings(api_key="secret"))
    client, _, _ = _reset_client(tmp_path, daemon_settings=settings, with_auth=True)
    async with client:
        r = await client.post("/api/v1/factory-reset", headers={"X-Api-Key": "secret"})
    assert r.status_code == 204


async def test_factory_reset_unauthenticated_by_default(tmp_path: Path) -> None:
    """No API key configured -> factory-reset stays reachable from the Portal."""
    client, _, _ = _reset_client(tmp_path)
    async with client:
        r = await client.post("/api/v1/factory-reset")
    assert r.status_code == 204


async def test_debug_bundle_requires_auth_when_configured(tmp_path: Path) -> None:
    settings = DaemonSettings(api=ApiSettings(api_key="secret"))
    async with _make_client(_READY, tmp_path, daemon_settings=settings, with_auth=True) as client:
        r = await client.get("/api/v1/debug/bundle")
    assert r.status_code == 401


async def test_debug_bundle_accepts_valid_api_key(tmp_path: Path) -> None:
    settings = DaemonSettings(api=ApiSettings(api_key="secret"))
    async with _make_client(_READY, tmp_path, daemon_settings=settings, with_auth=True) as client:
        r = await client.get("/api/v1/debug/bundle", headers={"X-Api-Key": "secret"})
    assert r.status_code == 200


async def test_debug_bundle_unauthenticated_by_default(tmp_path: Path) -> None:
    async with _make_client(_READY, tmp_path) as client:
        r = await client.get("/api/v1/debug/bundle")
    assert r.status_code == 200
