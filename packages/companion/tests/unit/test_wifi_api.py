"""Tests for WiFi provisioning REST endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest
from companion.services.provisioning import (
    ProvisioningFailureReason,
    ProvisioningService,
    ProvisioningState,
    ProvisioningStatus,
    WifiNetwork,
)
from companion.wifi.router import make_wifi_router
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(
    state: ProvisioningState = ProvisioningState.CONNECTED,
    ap_ip: str | None = None,
    networks: list[WifiNetwork] | None = None,
    reason: ProvisioningFailureReason | None = None,
    message: str | None = None,
) -> MagicMock:
    svc = MagicMock(spec=ProvisioningService)
    type(svc).status = PropertyMock(
        return_value=ProvisioningStatus(state=state, ap_ip=ap_ip, reason=reason, message=message)
    )
    svc.scan_networks = AsyncMock(return_value=networks or [])
    svc.request_connect = AsyncMock()
    return svc


def _make_client(svc: MagicMock) -> AsyncClient:
    app = FastAPI()
    app.include_router(make_wifi_router(svc))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# GET /api/v1/wifi/status — state and reason
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_connected() -> None:
    svc = _make_service(state=ProvisioningState.CONNECTED)
    async with _make_client(svc) as client:
        r = await client.get("/api/v1/wifi/status")
    assert r.status_code == 200
    data = r.json()
    assert data["state"] == "connected"
    assert data["ap_ip"] is None
    assert data["reason"] is None
    assert data["message"] is None


@pytest.mark.asyncio
async def test_status_ap_active_no_failure() -> None:
    svc = _make_service(state=ProvisioningState.AP_ACTIVE, ap_ip="10.42.0.1")
    async with _make_client(svc) as client:
        r = await client.get("/api/v1/wifi/status")
    data = r.json()
    assert data["state"] == "ap_active"
    assert data["ap_ip"] == "10.42.0.1"
    assert data["reason"] is None
    assert data["message"] is None


@pytest.mark.asyncio
async def test_status_ap_active_with_auth_failure() -> None:
    svc = _make_service(
        state=ProvisioningState.AP_ACTIVE,
        ap_ip="10.42.0.1",
        reason=ProvisioningFailureReason.AUTHENTICATION_FAILED,
        message="Incorrect WiFi password.",
    )
    async with _make_client(svc) as client:
        r = await client.get("/api/v1/wifi/status")
    data = r.json()
    assert data["state"] == "ap_active"
    assert data["reason"] == "authentication_failed"
    assert data["message"] == "Incorrect WiFi password."


@pytest.mark.asyncio
async def test_status_ap_active_with_timeout() -> None:
    svc = _make_service(
        state=ProvisioningState.AP_ACTIVE,
        ap_ip="10.42.0.1",
        reason=ProvisioningFailureReason.TIMEOUT,
        message="Connection timed out. Move closer to your router and try again.",
    )
    async with _make_client(svc) as client:
        r = await client.get("/api/v1/wifi/status")
    data = r.json()
    assert data["reason"] == "timeout"


@pytest.mark.asyncio
async def test_status_unprovisioned() -> None:
    svc = _make_service(state=ProvisioningState.UNPROVISIONED)
    async with _make_client(svc) as client:
        r = await client.get("/api/v1/wifi/status")
    assert r.json()["state"] == "unprovisioned"


# ---------------------------------------------------------------------------
# GET /api/v1/wifi/networks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_networks_returns_list() -> None:
    nets = [
        WifiNetwork(ssid="HomeNet", signal=85, security="WPA2"),
        WifiNetwork(ssid="GuestNet", signal=40, security=""),
    ]
    svc = _make_service(networks=nets)
    async with _make_client(svc) as client:
        r = await client.get("/api/v1/wifi/networks")
    assert r.status_code == 200
    data = r.json()
    assert len(data["networks"]) == 2
    assert data["networks"][0]["ssid"] == "HomeNet"
    assert data["networks"][0]["signal"] == 85
    assert data["networks"][0]["security"] == "WPA2"
    assert data["networks"][1]["security"] == ""


@pytest.mark.asyncio
async def test_networks_empty() -> None:
    svc = _make_service(networks=[])
    async with _make_client(svc) as client:
        r = await client.get("/api/v1/wifi/networks")
    assert r.status_code == 200
    assert r.json()["networks"] == []


# ---------------------------------------------------------------------------
# POST /api/v1/wifi/connect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_with_password() -> None:
    svc = _make_service()
    async with _make_client(svc) as client:
        r = await client.post(
            "/api/v1/wifi/connect",
            json={"ssid": "HomeNet", "password": "s3cr3t"},
        )
    assert r.status_code == 204
    svc.request_connect.assert_awaited_once_with("HomeNet", "s3cr3t")


@pytest.mark.asyncio
async def test_connect_open_network() -> None:
    svc = _make_service()
    async with _make_client(svc) as client:
        r = await client.post(
            "/api/v1/wifi/connect",
            json={"ssid": "GuestNet", "password": None},
        )
    assert r.status_code == 204
    svc.request_connect.assert_awaited_once_with("GuestNet", None)


@pytest.mark.asyncio
async def test_connect_missing_ssid_returns_422() -> None:
    svc = _make_service()
    async with _make_client(svc) as client:
        r = await client.post("/api/v1/wifi/connect", json={"password": "pw"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_connect_password_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    svc = _make_service()
    async with _make_client(svc) as client:
        with caplog.at_level(logging.DEBUG):
            await client.post(
                "/api/v1/wifi/connect",
                json={"ssid": "HomeNet", "password": "supersecret"},
            )
    assert "supersecret" not in caplog.text
