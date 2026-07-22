"""WiFi provisioning REST endpoints."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel

from companion.services.provisioning import ProvisioningService, ProvisioningState


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


def make_wifi_router(
    provisioning: ProvisioningService,
    auth: Callable[..., Awaitable[None]] | None = None,
) -> APIRouter:
    """Return an APIRouter with WiFi provisioning endpoints.

    ``GET /status`` and ``GET /networks`` stay unauthenticated -- they're
    read-only and provisioning runs before any API key can be entered.

    ``POST /connect`` requires *auth* (when configured) once the appliance is
    on its home network (``ProvisioningState.CONNECTED``) -- an unauthenticated
    caller that already knows the LAN can otherwise redirect the appliance to
    an attacker's WiFi network (SEC-02). Auth is bypassed in every other
    state (``unprovisioned``, ``ap_active``, ``connecting``) so the
    provisioning flow itself -- run before any API key can be supplied,
    over the appliance's own open AP -- keeps working with no key entry step.
    """
    router = APIRouter(prefix="/api/v1/wifi", tags=["wifi"])

    async def require_auth_once_connected(
        x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    ) -> None:
        if auth is None or provisioning.status.state != ProvisioningState.CONNECTED:
            return
        await auth(x_api_key)

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
        dependencies=[Depends(require_auth_once_connected)],
    )
    async def post_wifi_connect(body: WifiConnectRequest) -> None:
        """Submit an SSID and optional password to connect to a WiFi network.

        Returns 204 immediately. Poll ``GET /api/v1/wifi/status`` to track
        progress. On failure, the service restores the AP and sets ``reason``
        and ``message`` on the status endpoint.

        Requires authentication once the appliance is already on its home
        network (SEC-02) -- an unauthenticated call could otherwise redirect
        it onto an attacker-controlled WiFi network. Unauthenticated during
        the provisioning flow itself (``unprovisioned``/``ap_active``/
        ``connecting``), which runs before any API key can be entered.
        """
        await provisioning.request_connect(body.ssid, body.password)

    return router
