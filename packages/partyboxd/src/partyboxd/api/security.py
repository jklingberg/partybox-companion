"""Host/Origin allowlist middleware — closes the CSRF/DNS-rebinding gap.

See ``docs/adr/041-host-origin-allowlist.md`` and GitHub issue #75
(SEC-02/SEC-04).
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

#: Simple requests with these methods can mutate appliance state, so their
#: Origin (when a browser sends one) is checked in addition to Host. WebSocket
#: handshakes are Origin-checked unconditionally, regardless of this set —
#: see HostOriginMiddleware's docstring.
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
    """Extract the lowercase hostname from a Host header or Origin URL value.

    Strips a single trailing dot (``partybox.local.`` is the same DNS name as
    ``partybox.local`` — the trailing dot denotes the DNS root and is dropped
    by resolvers before lookup) so a client or resolver that includes it
    doesn't get rejected as if it were a foreign host.
    """
    if "://" in value:
        value = urlsplit(value).netloc
    if value.startswith("["):
        return value.split("]")[0].lstrip("[").lower()
    return value.rsplit(":", 1)[0].rstrip(".").lower()


class HostOriginMiddleware:
    """Reject requests whose Host/Origin doesn't identify this appliance.

    Defeats DNS rebinding (SEC-04): a malicious page can rebind its own
    hostname to the appliance's LAN IP, but the browser still sends *its own*
    hostname in the Host header, not the appliance's, so it never matches.

    Also defeats browser-driven CSRF (SEC-02) on mutating requests: a
    cross-origin ``fetch(..., {method: "POST"})`` carries an ``Origin`` header
    for the page that issued it, which likewise won't match.

    Origin is also checked on every WebSocket handshake, mutating method or
    not: a WS handshake scope has no ``method`` key, and — unlike ``fetch`` —
    a cross-origin ``new WebSocket(...)`` is not blocked by the browser's
    same-origin policy at all, so a foreign Origin there is itself the
    CSRF-equivalent signal, not something gated behind a method check.

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

        # Read header occurrences as a list rather than collapsing to a dict:
        # a naive dict(scope["headers"]) silently keeps only the *last*
        # occurrence of a repeated header, so a request smuggling two Host (or
        # two Origin) headers -- a forged one followed by a matching one --
        # would sail through. ASGI servers are not guaranteed to reject
        # duplicate Host headers themselves (verified against uvicorn/h11,
        # which pass both through), so this middleware must treat more than
        # one occurrence of either header as ambiguous and reject it, per
        # RFC 7230 §5.4's requirement for exactly one Host header.
        raw_headers: list[tuple[bytes, bytes]] = scope["headers"]
        server = scope.get("server")
        allowed = _ALLOWED_HOSTNAMES | ({server[0].lower()} if server else set())

        hosts = [v for k, v in raw_headers if k == b"host"]
        if len(hosts) != 1 or _hostname(hosts[0].decode("latin-1")) not in allowed:
            await self._reject(scope, receive, send)
            return

        if scope["type"] == "websocket" or scope.get("method", "") in _MUTATING_METHODS:
            origins = [v for k, v in raw_headers if k == b"origin"]
            if len(origins) > 1:
                await self._reject(scope, receive, send)
                return
            if origins and _hostname(origins[0].decode("latin-1")) not in allowed:
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
