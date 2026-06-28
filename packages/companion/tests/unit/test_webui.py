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
from companion.webui.router import PortalConfig, make_portal_router
from httpx import ASGITransport, AsyncClient
from partyboxd.api import create_app as create_daemon_app
from partyboxd.config import Settings as DaemonSettings
from partyboxd.device.manager import StatusSnapshot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(tmp_path: Path) -> AsyncClient:
    """Assemble a companion app backed by a mock DeviceManager."""
    companion_settings = CompanionSettings(data_dir=tmp_path)
    daemon_settings = DaemonSettings()

    manager = MagicMock()
    type(manager).snapshot = PropertyMock(
        return_value=StatusSnapshot(connected=False, address=None, firmware=None, battery=None)
    )
    manager.subscribe = MagicMock(return_value=asyncio.Queue())
    manager.unsubscribe = MagicMock()

    app = create_daemon_app(manager, daemon_settings)
    app.include_router(make_portal_router(companion_settings))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# GET /api/v1/config
# ---------------------------------------------------------------------------


async def test_get_config_returns_defaults_on_first_boot(tmp_path: Path) -> None:
    async with _make_app(tmp_path) as client:
        r = await client.get("/api/v1/config")
    assert r.status_code == 200
    assert r.json()["device_name"] == "PartyBox"


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
        r = await client.put("/api/v1/config", json={"device_name": "Kitchen"})
    assert r.status_code == 200
    assert r.json()["device_name"] == "Kitchen"


async def test_put_config_writes_to_disk(tmp_path: Path) -> None:
    async with _make_app(tmp_path) as client:
        await client.put("/api/v1/config", json={"device_name": "Garage"})
    config_file = tmp_path / "config.json"
    assert config_file.exists()
    cfg = PortalConfig.model_validate_json(config_file.read_text())
    assert cfg.device_name == "Garage"


async def test_config_persists_across_requests(tmp_path: Path) -> None:
    async with _make_app(tmp_path) as client:
        await client.put("/api/v1/config", json={"device_name": "Living Room"})
        r = await client.get("/api/v1/config")
    assert r.json()["device_name"] == "Living Room"


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
    assert "/api/v1/config" in html
    assert "/api/v1/events" in html
    assert "/api/v1/power/" in html


async def test_portal_html_contains_product_name(tmp_path: Path) -> None:
    async with _make_app(tmp_path) as client:
        r = await client.get("/")
    assert "PartyBox" in r.text


async def test_portal_html_contains_streaming_placeholders(tmp_path: Path) -> None:
    async with _make_app(tmp_path) as client:
        r = await client.get("/")
    html = r.text
    assert "Spotify" in html
    assert "AirPlay" in html


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
