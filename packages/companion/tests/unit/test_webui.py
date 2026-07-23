"""Tests for the Companion Portal — config API and HTML serving.

All tests use a mock DeviceManager and a tmp_path for config storage;
no Bluetooth hardware is required.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock

import pytest
from companion.config import CompanionSettings
from companion.config_store import ConfigStore, PortalConfig
from companion.webui.router import make_portal_router
from httpx import ASGITransport, AsyncClient
from partyboxd.api import create_app as create_daemon_app
from partyboxd.api.auth import make_auth_dependency
from partyboxd.config import ApiSettings
from partyboxd.config import Settings as DaemonSettings
from partyboxd.device.manager import StatusSnapshot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(
    tmp_path: Path,
    *,
    daemon_settings: DaemonSettings | None = None,
    with_auth: bool = False,
) -> AsyncClient:
    """Assemble a companion app backed by a mock DeviceManager."""
    companion_settings = CompanionSettings(data_dir=tmp_path)
    settings = daemon_settings or DaemonSettings()
    store = ConfigStore(tmp_path / "config.json")

    manager = MagicMock()
    type(manager).snapshot = PropertyMock(
        return_value=StatusSnapshot(connected=False, address=None, firmware=None, battery=None)
    )
    manager.subscribe = MagicMock(return_value=asyncio.Queue())
    manager.unsubscribe = MagicMock()

    app = create_daemon_app(manager, settings)
    app.include_router(
        make_portal_router(
            companion_settings, store, auth=make_auth_dependency(settings) if with_auth else None
        )
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# GET /api/v1/config
# ---------------------------------------------------------------------------


async def test_get_config_returns_defaults_on_first_boot(tmp_path: Path) -> None:
    async with _make_app(tmp_path) as client:
        r = await client.get("/api/v1/config")
    assert r.status_code == 200
    body = r.json()
    assert body["spotify_connect_name"] == "PartyBox"
    assert body["spotify_bitrate"] == 320


async def test_get_config_always_200_unauthenticated(tmp_path: Path) -> None:
    """Config is public — no X-Api-Key header needed."""
    async with _make_app(tmp_path) as client:
        r = await client.get("/api/v1/config")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# PUT /api/v1/config
# ---------------------------------------------------------------------------


async def test_put_config_returns_updated_body(tmp_path: Path) -> None:
    async with _make_app(tmp_path) as client:
        r = await client.put(
            "/api/v1/config",
            json={
                "spotify_connect_name": "Kitchen",
                "spotify_bitrate": 160,
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body["spotify_connect_name"] == "Kitchen"
    assert body["spotify_bitrate"] == 160


async def test_put_config_writes_to_disk(tmp_path: Path) -> None:
    async with _make_app(tmp_path) as client:
        await client.put(
            "/api/v1/config",
            json={
                "spotify_connect_name": "Garage",
                "spotify_bitrate": 320,
            },
        )
    config_file = tmp_path / "config.json"
    assert config_file.exists()
    cfg = PortalConfig.model_validate_json(config_file.read_text())
    assert cfg.spotify_connect_name == "Garage"


async def test_config_persists_across_requests(tmp_path: Path) -> None:
    async with _make_app(tmp_path) as client:
        await client.put(
            "/api/v1/config",
            json={
                "spotify_connect_name": "Living Room",
                "spotify_bitrate": 320,
            },
        )
        r = await client.get("/api/v1/config")
    assert r.json()["spotify_connect_name"] == "Living Room"


async def test_put_config_accepts_partial_with_defaults(tmp_path: Path) -> None:
    """PUT with only spotify_connect_name — other fields take defaults."""
    async with _make_app(tmp_path) as client:
        r = await client.put("/api/v1/config", json={"spotify_connect_name": "Den"})
    assert r.status_code == 200
    body = r.json()
    assert body["spotify_connect_name"] == "Den"
    assert body["spotify_bitrate"] == 320


# ---------------------------------------------------------------------------
# PUT /api/v1/config — auth (SEC-02)
# ---------------------------------------------------------------------------


async def test_put_config_requires_auth_when_configured(tmp_path: Path) -> None:
    settings = DaemonSettings(api=ApiSettings(api_key="secret"))
    async with _make_app(tmp_path, daemon_settings=settings, with_auth=True) as client:
        r = await client.put("/api/v1/config", json={"spotify_connect_name": "Den"})
    assert r.status_code == 401


async def test_put_config_accepts_valid_api_key(tmp_path: Path) -> None:
    settings = DaemonSettings(api=ApiSettings(api_key="secret"))
    async with _make_app(tmp_path, daemon_settings=settings, with_auth=True) as client:
        r = await client.put(
            "/api/v1/config",
            json={"spotify_connect_name": "Den"},
            headers={"X-Api-Key": "secret"},
        )
    assert r.status_code == 200


async def test_get_config_does_not_require_auth_when_configured(tmp_path: Path) -> None:
    """GET /api/v1/config stays public even with an API key configured."""
    settings = DaemonSettings(api=ApiSettings(api_key="secret"))
    async with _make_app(tmp_path, daemon_settings=settings, with_auth=True) as client:
        r = await client.get("/api/v1/config")
    assert r.status_code == 200


async def test_put_config_unauthenticated_by_default(tmp_path: Path) -> None:
    """No API key configured -> Portal settings save keeps working with no key."""
    async with _make_app(tmp_path) as client:
        r = await client.put("/api/v1/config", json={"spotify_connect_name": "Den"})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# GET / — Portal HTML
# ---------------------------------------------------------------------------


async def test_portal_root_returns_html(tmp_path: Path) -> None:
    async with _make_app(tmp_path) as client:
        r = await client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")


async def test_portal_html_references_api_endpoints(tmp_path: Path) -> None:
    async with _make_app(tmp_path) as client:
        r = await client.get("/")
    html = r.text
    assert "/api/v1/health" in html
    assert "/api/v1/health/details" in html
    assert "/api/v1/config" in html
    assert "/api/v1/events" in html
    assert "/api/v1/power/" in html
    assert "/api/v1/debug/bundle" in html
    assert "/api/v1/spotify/restart" in html


async def test_portal_html_contains_product_name(tmp_path: Path) -> None:
    async with _make_app(tmp_path) as client:
        r = await client.get("/")
    assert "PartyBox" in r.text


async def test_portal_html_contains_spotify_source_card(tmp_path: Path) -> None:
    async with _make_app(tmp_path) as client:
        r = await client.get("/")
    html = r.text
    assert "Spotify Connect" in html
    # AirPlay has no placeholder row — it appears only once the feature ships
    # (progressive disclosure; see docs/design/portal-redesign.md §10).
    assert "AirPlay" not in html


async def test_portal_html_contains_health_sheet(tmp_path: Path) -> None:
    async with _make_app(tmp_path) as client:
        r = await client.get("/")
    assert "System health" in r.text


async def test_portal_html_contains_settings_sections(tmp_path: Path) -> None:
    async with _make_app(tmp_path) as client:
        r = await client.get("/")
    html = r.text
    assert "spotify_connect_name" in html
    assert "spotify_bitrate" in html
    assert "debug/bundle" in html


async def test_portal_html_factory_reset_uses_typed_inline_confirm(tmp_path: Path) -> None:
    """Factory reset must not use a native window.confirm() dialog.

    docs/design/portal-redesign.md §12 item 6: restyled as an in-sheet typed
    confirmation step in the settings sheet's own visual language.
    """
    async with _make_app(tmp_path) as client:
        r = await client.get("/")
    html = r.text
    assert "factory-reset-confirm" in html
    assert "confirm(" not in html


# ---------------------------------------------------------------------------
# Smoke: daemon API still reachable alongside the Portal
# ---------------------------------------------------------------------------


async def test_daemon_health_still_reachable(tmp_path: Path) -> None:
    """The daemon's /health endpoint coexists with the Portal."""
    async with _make_app(tmp_path) as client:
        r = await client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# ConfigStore
# ---------------------------------------------------------------------------


def test_config_store_returns_defaults_when_no_file(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    cfg = store.read()
    assert cfg.spotify_connect_name == "PartyBox"
    assert cfg.spotify_bitrate == 320


def test_config_store_roundtrip(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    original = PortalConfig(spotify_connect_name="Patio Speaker", spotify_bitrate=160)
    store.write(original)
    loaded = store.read()
    assert loaded.spotify_connect_name == "Patio Speaker"
    assert loaded.spotify_bitrate == 160


def test_config_store_creates_parent_dirs(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "config.json"
    store = ConfigStore(nested)
    store.write(PortalConfig())
    assert nested.exists()


# ---------------------------------------------------------------------------
# CompanionSettings
# ---------------------------------------------------------------------------


def test_companion_settings_defaults() -> None:
    s = CompanionSettings()
    assert s.host == "0.0.0.0"  # noqa: S104
    assert s.port == 8080
    assert s.data_dir.name == "companion"


@pytest.mark.parametrize("port", [80, 443, 8080, 9000])
def test_companion_settings_port_range(port: int) -> None:
    s = CompanionSettings(port=port)
    assert s.port == port
