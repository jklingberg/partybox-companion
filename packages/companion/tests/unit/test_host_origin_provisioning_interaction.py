"""HostOriginMiddleware (SEC-02/SEC-04) vs. CaptivePortalMiddleware ordering.

During AP-mode provisioning, iOS/Android captive-portal probes arrive with
Host set to the *probed* domain (e.g. connectivitycheck.gstatic.com, resolved
to the AP IP by wildcard DNS) -- structurally identical to a DNS-rebinding
request. HostOriginMiddleware alone would reject these, breaking the
auto-popup captive-portal flow ADR-021 relies on.

This is only safe because of middleware *ordering*: companion/__main__.py adds
CaptivePortalMiddleware via app.add_middleware() *after* create_daemon_app()
has already added HostOriginMiddleware, and Starlette's add_middleware()
inserts at the front of the list it later wraps in reversed() order -- so the
most-recently-added middleware ends up outermost. CaptivePortalMiddleware
therefore intercepts and 302s known probe paths before HostOriginMiddleware
ever runs, while every other path still gets HostOriginMiddleware's
protection. See docs/adr/041-host-origin-allowlist.md.
"""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock

from companion.services.provisioning import (
    ProvisioningService,
    ProvisioningState,
    ProvisioningStatus,
)
from companion.wifi.middleware import CaptivePortalMiddleware
from httpx import ASGITransport, AsyncClient
from partyboxd.api import create_app as create_daemon_app
from partyboxd.config import Settings as DaemonSettings
from partyboxd.device.manager import StatusSnapshot


def _make_client(state: ProvisioningState, ap_ip: str | None) -> AsyncClient:
    manager = MagicMock()
    type(manager).snapshot = PropertyMock(
        return_value=StatusSnapshot(connected=False, address=None, firmware=None, battery=None)
    )
    # Mirrors companion/__main__.py: create_daemon_app() adds HostOriginMiddleware
    # internally, then CaptivePortalMiddleware is layered on afterwards.
    app = create_daemon_app(manager, DaemonSettings())

    provisioning = MagicMock(spec=ProvisioningService)
    type(provisioning).status = PropertyMock(
        return_value=ProvisioningStatus(state=state, ap_ip=ap_ip)
    )
    app.add_middleware(CaptivePortalMiddleware, provisioning=provisioning)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_captive_probe_redirected_despite_foreign_host_during_ap_mode() -> None:
    """The probe's own Host (a wildcard-DNS'd external domain) must not get 400."""
    async with _make_client(ProvisioningState.AP_ACTIVE, "10.42.0.1") as client:
        r = await client.get("/generate_204", headers={"Host": "connectivitycheck.gstatic.com"})
    assert r.status_code == 302
    assert r.headers["location"] == "http://10.42.0.1/"


async def test_non_probe_path_with_foreign_host_still_rejected_during_ap_mode() -> None:
    """Captive-portal mode doesn't blanket-disable Host validation for everything."""
    async with _make_client(ProvisioningState.AP_ACTIVE, "10.42.0.1") as client:
        r = await client.get("/api/v1/health", headers={"Host": "evil.example.com"})
    assert r.status_code == 400


async def test_probe_path_outside_ap_mode_gets_normal_host_check() -> None:
    """Outside provisioning, CaptivePortalMiddleware passes probes straight through
    to HostOriginMiddleware -- a foreign Host on a probe path is then rejected."""
    async with _make_client(ProvisioningState.CONNECTED, None) as client:
        r = await client.get("/generate_204", headers={"Host": "connectivitycheck.gstatic.com"})
    assert r.status_code == 400
