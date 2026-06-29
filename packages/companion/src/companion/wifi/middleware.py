"""Captive portal probe interception middleware.

During AP mode, iOS and Android probe well-known URLs to detect captive
portals. This middleware intercepts those probes and returns HTTP 302 to the
Portal root, which triggers the OS to open its Captive Network Assistant
(CNA) popup.

Returning 302 (not 204): a 204 response signals "no captive portal, network
is open" -- the OS suppresses its popup. A 302 redirect to the Portal is what
causes the popup to appear.

The middleware is active on every request but only fires when the
ProvisioningService reports AP_ACTIVE state. All other requests pass through
unmodified.
"""

from __future__ import annotations

from starlette.responses import RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from companion.services.provisioning import ProvisioningService, ProvisioningState

# Well-known paths probed by iOS, Android, Windows, and ChromeOS to detect
# captive portals. All of them are intercepted during provisioning mode.
_PROBE_PATHS = frozenset(
    {
        "/generate_204",
        "/hotspot-detect.html",
        "/library/test/success.html",
        "/connecttest.txt",
        "/ncsi.txt",
        "/redirect",
        "/success.txt",
        "/mobile/status.php",
        "/chat",
    }
)


class CaptivePortalMiddleware:
    """ASGI middleware that intercepts captive portal probes during AP mode."""

    def __init__(self, app: ASGIApp, provisioning: ProvisioningService) -> None:
        self._app = app
        self._provisioning = provisioning

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path = str(scope.get("path", ""))
            if (
                self._provisioning.status.state == ProvisioningState.AP_ACTIVE
                and path in _PROBE_PATHS
            ):
                ap_ip = self._provisioning.status.ap_ip or "10.42.0.1"
                response: Response = RedirectResponse(f"http://{ap_ip}/", status_code=302)
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)
