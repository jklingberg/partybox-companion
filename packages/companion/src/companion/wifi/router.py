"""WiFi provisioning REST endpoints."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from companion.services.provisioning import ProvisioningService


class WifiStatusResponse(BaseModel):
    state: str
    ap_ip: str | None = None
    reason: str | None = None
    message: str | None = None


class WifiNetworkModel(BaseModel):
    ssid: str
    signal: int
    security: str


class WifiNetworksResponse(BaseModel):
    networks: list[WifiNetworkModel]


class WifiConnectRequest(BaseModel):
    ssid: str
    password: str | None = None


def make_wifi_router(provisioning: ProvisioningService) -> APIRouter:
    """Return an APIRouter with WiFi provisioning endpoints.

    All endpoints are unauthenticated -- provisioning runs before any API key
    is configured and before the appliance has network connectivity.
    """
    router = APIRouter(prefix="/api/v1/wifi", tags=["wifi"])

    @router.get(
        "/status",
        response_model=WifiStatusResponse,
        summary="WiFi provisioning state",
    )
    async def get_wifi_status() -> WifiStatusResponse:
        """Current WiFi provisioning state.

        Returns one of: ``unprovisioned``, ``ap_active``, ``connecting``,
        ``connected``. When the previous connection attempt failed, ``reason``
        is one of: ``authentication_failed``, ``timeout``, ``not_found``,
        ``unknown``. ``message`` is a human-readable string suitable for the UI.
        """
        s = provisioning.status
        return WifiStatusResponse(
            state=s.state.value,
            ap_ip=s.ap_ip,
            reason=s.reason.value if s.reason else None,
            message=s.message,
        )

    @router.get(
        "/networks",
        response_model=WifiNetworksResponse,
        summary="Visible WiFi networks",
    )
    async def get_wifi_networks() -> WifiNetworksResponse:
        """Return nearby WiFi networks sorted by signal strength."""
        networks = await provisioning.scan_networks()
        return WifiNetworksResponse(
            networks=[
                WifiNetworkModel(ssid=n.ssid, signal=n.signal, security=n.security)
                for n in networks
            ]
        )

    @router.post(
        "/connect",
        status_code=204,
        summary="Submit WiFi credentials",
    )
    async def post_wifi_connect(body: WifiConnectRequest) -> None:
        """Submit an SSID and optional password to connect to a WiFi network.

        Returns 204 immediately. Poll ``GET /api/v1/wifi/status`` to track
        progress. On failure, the service restores the AP and sets ``reason``
        and ``message`` on the status endpoint.
        """
        await provisioning.request_connect(body.ssid, body.password)

    return router
