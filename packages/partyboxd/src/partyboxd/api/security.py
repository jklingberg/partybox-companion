"""Host/Origin allowlist middleware — closes the CSRF/DNS-rebinding gap.

See ``docs/adr/041-host-origin-allowlist.md`` and GitHub issue #75
(SEC-02/SEC-04).
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

#: Simple requests with these methods can mutate appliance state, so their
#: Origin (when a browser sends one) is checked in addition to Host.
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Static allowlist. Anything else is only accepted if it matches the address
#: this specific connection actually reached the server on (see below) — that
#: covers the DHCP-assigned LAN IP, a router reservation, or the provisioning
#: AP's fixed 10.42.0.1, without hardcoding any of them.
_ALLOWED_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1", "partybox", "partybox.local"})

_REJECTED_BODY = (
    b'{"error":"untrusted_origin","message":"Request Host/Origin header not recognized."}'
)


def _hostname(value: str) -> str:
    """Extract the lowercase hostname from a Host header or Origin URL value."""
    if "://" in value:
        value = urlsplit(value).netloc
    if value.startswith("["):
        return value.split("]")[0].lstrip("[").lower()
    return value.rsplit(":", 1)[0].lower()


class HostOriginMiddleware:
    """Reject requests whose Host/Origin doesn't identify this appliance.

    Defeats DNS rebinding (SEC-04): a malicious page can rebind its own
    hostname to the appliance's LAN IP, but the browser still sends *its own*
    hostname in the Host header, not the appliance's, so it never matches.

    Also defeats browser-driven CSRF (SEC-02) on mutating requests: a
    cross-origin ``fetch(..., {method: "POST"})`` carries an ``Origin`` header
    for the page that issued it, which likewise won't match.

    ``scope["server"][0]`` is the literal address this connection reached the
    server on (uvicorn populates it from the socket, not from the Host
    header), so comparing against it allows any direct-IP access — current
    DHCP lease, a router reservation, or the provisioning AP's IP — with no
    configuration and no special-casing for provisioning mode.

    Non-browser clients (curl, the hardware test suite, `journalctl`-adjacent
    tooling) send no Origin header at all; its absence is not treated as
    suspicious, only a *mismatched* Origin is rejected.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return

        headers = dict(scope["headers"])
        server = scope.get("server")
        allowed = _ALLOWED_HOSTNAMES | ({server[0].lower()} if server else set())

        host = headers.get(b"host")
        if host is None or _hostname(host.decode("latin-1")) not in allowed:
            await self._reject(scope, receive, send)
            return

        if scope.get("method", "") in _MUTATING_METHODS:
            origin = headers.get(b"origin")
            if origin is not None and _hostname(origin.decode("latin-1")) not in allowed:
                await self._reject(scope, receive, send)
                return

        await self._app(scope, receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            # Handshakes must be refused via the WS close protocol, not a
            # plain HTTP response — there is no HTTP response to send once a
            # connection has upgraded.
            await receive()  # the "websocket.connect" event
            await send({"type": "websocket.close", "code": 4403})
            return
        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": _REJECTED_BODY})
