"""Tests for HostOriginMiddleware — SEC-02/SEC-04 (DNS rebinding + CSRF)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock

from httpx import ASGITransport, AsyncClient
from partyboxd.api import create_app
from partyboxd.config import ApiSettings, Settings
from partyboxd.device.manager import StatusSnapshot

_CONNECTED = StatusSnapshot(
    connected=True, address="AA:BB:CC:DD:EE:FF", firmware="26.2.10", battery=None
)


def _make_client(settings: Settings | None = None) -> AsyncClient:
    manager = MagicMock()
    type(manager).snapshot = PropertyMock(return_value=_CONNECTED)
    manager.power_on = AsyncMock()
    manager.power_off = AsyncMock()
    app = create_app(manager, settings or Settings())
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# Host header — DNS rebinding (SEC-04)
# ---------------------------------------------------------------------------


async def test_allowed_host_passes_through() -> None:
    """base_url="http://test" sends Host: test, which matches scope["server"]."""
    async with _make_client() as client:
        r = await client.get("/api/v1/health")
    assert r.status_code == 200


async def test_rebound_host_rejected_on_get() -> None:
    """A DNS-rebound hostname in Host must not reach the app, even for GET."""
    async with _make_client() as client:
        r = await client.get("/api/v1/health", headers={"Host": "evil.example.com"})
    assert r.status_code == 400


async def test_partybox_local_hostname_allowed() -> None:
    async with _make_client() as client:
        r = await client.get("/api/v1/health", headers={"Host": "partybox.local"})
    assert r.status_code == 200


async def test_partybox_bare_hostname_allowed() -> None:
    async with _make_client() as client:
        r = await client.get("/api/v1/health", headers={"Host": "partybox"})
    assert r.status_code == 200


async def test_localhost_allowed() -> None:
    async with _make_client() as client:
        r = await client.get("/api/v1/health", headers={"Host": "localhost:8080"})
    assert r.status_code == 200


async def test_missing_host_header_rejected() -> None:
    """httpx always sends a Host header, so exercise the ASGI layer directly."""
    from partyboxd.api.security import HostOriginMiddleware

    events: list[dict[str, object]] = []

    async def app(scope: object, receive: object, send: object) -> None:
        raise AssertionError("downstream app must not run for a request with no Host header")

    async def send(message: dict[str, object]) -> None:
        events.append(message)

    async def receive() -> dict[str, object]:
        raise AssertionError("not expected for a plain http request")

    middleware = HostOriginMiddleware(app)
    scope = {
        "type": "http",
        "method": "GET",
        "headers": [],
        "server": ("192.168.1.50", 8080),
    }
    await middleware(scope, receive, send)
    assert events[0]["status"] == 400


async def test_trailing_dot_host_allowed() -> None:
    """ "partybox.local." is the same DNS name as "partybox.local" (root label)."""
    async with _make_client() as client:
        r = await client.get("/api/v1/health", headers={"Host": "partybox.local."})
    assert r.status_code == 200


async def test_empty_host_value_rejected() -> None:
    """httpx rejects an empty header value outright, so exercise the ASGI layer."""
    from partyboxd.api.security import HostOriginMiddleware

    events: list[dict[str, object]] = []

    async def app(scope: object, receive: object, send: object) -> None:
        raise AssertionError("downstream app must not run for an empty Host header")

    async def send(message: dict[str, object]) -> None:
        events.append(message)

    async def receive() -> dict[str, object]:
        raise AssertionError("not expected for a plain http request")

    middleware = HostOriginMiddleware(app)
    scope = {
        "type": "http",
        "method": "GET",
        "headers": [(b"host", b"")],
        "server": ("192.168.1.50", 8080),
    }
    await middleware(scope, receive, send)
    assert events[0]["status"] == 400


# ---------------------------------------------------------------------------
# Duplicate Host/Origin headers — a naive dict(scope["headers"]) silently
# keeps only the *last* occurrence of a repeated header. uvicorn/h11 do not
# reject requests with more than one Host header themselves (verified against
# a live server), so a request smuggling two Host headers -- forged first,
# legitimate last -- would otherwise sail through. RFC 7230 §5.4 requires
# rejecting any request with more than one Host header outright.
# ---------------------------------------------------------------------------


async def test_duplicate_host_forged_first_legit_last_rejected() -> None:
    """Regression: this exact ordering used to bypass the check via dict()."""
    from partyboxd.api.security import HostOriginMiddleware

    events: list[dict[str, object]] = []

    async def app(scope: object, receive: object, send: object) -> None:
        raise AssertionError("downstream app must not run for an ambiguous duplicate Host")

    async def send(message: dict[str, object]) -> None:
        events.append(message)

    async def receive() -> dict[str, object]:
        raise AssertionError("not expected for a plain http request")

    middleware = HostOriginMiddleware(app)
    scope = {
        "type": "http",
        "method": "GET",
        "headers": [(b"host", b"evil.example.com"), (b"host", b"partybox.local")],
        "server": ("192.168.1.50", 8080),
    }
    await middleware(scope, receive, send)
    assert events[0]["status"] == 400


async def test_duplicate_host_legit_first_forged_last_rejected() -> None:
    from partyboxd.api.security import HostOriginMiddleware

    events: list[dict[str, object]] = []

    async def app(scope: object, receive: object, send: object) -> None:
        raise AssertionError("downstream app must not run for an ambiguous duplicate Host")

    async def send(message: dict[str, object]) -> None:
        events.append(message)

    async def receive() -> dict[str, object]:
        raise AssertionError("not expected for a plain http request")

    middleware = HostOriginMiddleware(app)
    scope = {
        "type": "http",
        "method": "GET",
        "headers": [(b"host", b"partybox.local"), (b"host", b"evil.example.com")],
        "server": ("192.168.1.50", 8080),
    }
    await middleware(scope, receive, send)
    assert events[0]["status"] == 400


async def test_duplicate_origin_forged_first_legit_last_rejected() -> None:
    """Regression: this exact ordering used to bypass the CSRF check via dict()."""
    from partyboxd.api.security import HostOriginMiddleware

    events: list[dict[str, object]] = []

    async def app(scope: object, receive: object, send: object) -> None:
        raise AssertionError("downstream app must not run for an ambiguous duplicate Origin")

    async def send(message: dict[str, object]) -> None:
        events.append(message)

    async def receive() -> dict[str, object]:
        raise AssertionError("not expected for a plain http request")

    middleware = HostOriginMiddleware(app)
    scope = {
        "type": "http",
        "method": "POST",
        "headers": [
            (b"host", b"partybox.local"),
            (b"origin", b"https://evil.example.com"),
            (b"origin", b"http://partybox.local"),
        ],
        "server": ("192.168.1.50", 8080),
    }
    await middleware(scope, receive, send)
    assert events[0]["status"] == 400


# ---------------------------------------------------------------------------
# Origin header — CSRF (SEC-02), mutating methods only
# ---------------------------------------------------------------------------


async def test_forged_origin_rejected_on_post() -> None:
    """A legit Host (matches server) with a forged Origin is CSRF -- reject."""
    settings = Settings(api=ApiSettings(api_key=None))
    async with _make_client(settings) as client:
        r = await client.post(
            "/api/v1/power/on",
            headers={"Origin": "https://evil.example.com"},
        )
    assert r.status_code == 400


async def test_same_origin_post_allowed() -> None:
    async with _make_client() as client:
        r = await client.post("/api/v1/power/on", headers={"Origin": "http://test"})
    assert r.status_code == 204


async def test_missing_origin_on_post_allowed() -> None:
    """Non-browser clients (curl, this test suite) send no Origin at all."""
    async with _make_client() as client:
        r = await client.post("/api/v1/power/on")
    assert r.status_code == 204


async def test_forged_origin_does_not_affect_get() -> None:
    """Origin is only checked for mutating methods; GET ignores it."""
    async with _make_client() as client:
        r = await client.get(
            "/api/v1/health",
            headers={"Origin": "https://evil.example.com"},
        )
    assert r.status_code == 200


async def test_same_origin_post_with_port_allowed() -> None:
    """An Origin carrying an explicit port must still match on hostname alone."""
    async with _make_client() as client:
        r = await client.post("/api/v1/power/on", headers={"Origin": "http://test:8080"})
    assert r.status_code == 204


async def test_ipv6_origin_matches_ipv6_server_address() -> None:
    """A bracketed IPv6 Origin (as browsers serialize it) must match scope["server"]."""
    from partyboxd.api.security import HostOriginMiddleware

    app_called = False

    async def app(scope: object, receive: object, send: object) -> None:
        nonlocal app_called
        app_called = True

    async def send(message: dict[str, object]) -> None:
        pass

    async def receive() -> dict[str, object]:
        raise AssertionError("not expected for a plain http request")

    middleware = HostOriginMiddleware(app)
    scope = {
        "type": "http",
        "method": "POST",
        "headers": [
            (b"host", b"[fe80::1]:8080"),
            (b"origin", b"http://[fe80::1]:8080"),
        ],
        "server": ("fe80::1", 8080),
    }
    await middleware(scope, receive, send)
    assert app_called is True


async def test_malformed_origin_rejected_on_post() -> None:
    """An Origin that isn't a URL at all must not be treated as a match by accident."""
    async with _make_client() as client:
        r = await client.post("/api/v1/power/on", headers={"Origin": "not a url"})
    assert r.status_code == 400


async def test_empty_origin_value_rejected_on_post() -> None:
    """An explicitly empty Origin header is present (unlike a missing one) --
    treat it as a mismatch, not as the "no Origin sent" non-browser case."""
    from partyboxd.api.security import HostOriginMiddleware

    events: list[dict[str, object]] = []

    async def app(scope: object, receive: object, send: object) -> None:
        raise AssertionError("downstream app must not run for an empty Origin header")

    async def send(message: dict[str, object]) -> None:
        events.append(message)

    async def receive() -> dict[str, object]:
        raise AssertionError("not expected for a plain http request")

    middleware = HostOriginMiddleware(app)
    scope = {
        "type": "http",
        "method": "POST",
        "headers": [(b"host", b"partybox.local"), (b"origin", b"")],
        "server": ("192.168.1.50", 8080),
    }
    await middleware(scope, receive, send)
    assert events[0]["status"] == 400


# ---------------------------------------------------------------------------
# WebSocket handshakes — Origin is checked unconditionally, not just for
# "mutating methods" (a WS scope has no "method" key at all, and unlike
# fetch(), a cross-origin `new WebSocket(...)` isn't blocked by the browser's
# same-origin policy in the first place).
# ---------------------------------------------------------------------------


def _ws_scope(*, host: str = "test", origin: bytes | None = None) -> dict[str, object]:
    headers: list[tuple[bytes, bytes]] = [(b"host", host.encode())]
    if origin is not None:
        headers.append((b"origin", origin))
    return {
        "type": "websocket",
        "headers": headers,
        "server": ("test", None),
    }


async def test_forged_origin_rejected_on_websocket_handshake() -> None:
    """A cross-origin WebSocket handshake must be closed, not just left to the
    per-connection api_key query-param check in ws.py."""
    from partyboxd.api.security import HostOriginMiddleware

    app_called = False

    async def app(scope: object, receive: object, send: object) -> None:
        nonlocal app_called
        app_called = True

    events: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        events.append(message)

    async def receive() -> dict[str, object]:
        return {"type": "websocket.connect"}

    middleware = HostOriginMiddleware(app)
    scope = _ws_scope(origin=b"https://evil.example.com")
    await middleware(scope, receive, send)

    assert app_called is False
    assert events[0]["type"] == "websocket.close"
    assert events[0]["code"] == 4403


async def test_same_origin_websocket_handshake_allowed() -> None:
    from partyboxd.api.security import HostOriginMiddleware

    app_called = False

    async def app(scope: object, receive: object, send: object) -> None:
        nonlocal app_called
        app_called = True

    async def send(message: dict[str, object]) -> None:
        pass

    async def receive() -> dict[str, object]:
        raise AssertionError("must not be consumed when the handshake is allowed through")

    middleware = HostOriginMiddleware(app)
    scope = _ws_scope(origin=b"http://test")
    await middleware(scope, receive, send)

    assert app_called is True


async def test_missing_origin_on_websocket_handshake_allowed() -> None:
    """Non-browser WS clients (the hardware test suite, HA scripts) send no Origin."""
    from partyboxd.api.security import HostOriginMiddleware

    app_called = False

    async def app(scope: object, receive: object, send: object) -> None:
        nonlocal app_called
        app_called = True

    async def send(message: dict[str, object]) -> None:
        pass

    async def receive() -> dict[str, object]:
        raise AssertionError("must not be consumed when the handshake is allowed through")

    middleware = HostOriginMiddleware(app)
    scope = _ws_scope(origin=None)
    await middleware(scope, receive, send)

    assert app_called is True


async def test_websocket_bad_host_good_origin_rejected_at_host_check() -> None:
    """Host is checked first -- a rebound Host rejects the handshake even when
    Origin would otherwise have matched."""
    from partyboxd.api.security import HostOriginMiddleware

    app_called = False

    async def app(scope: object, receive: object, send: object) -> None:
        nonlocal app_called
        app_called = True

    events: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        events.append(message)

    async def receive() -> dict[str, object]:
        return {"type": "websocket.connect"}

    middleware = HostOriginMiddleware(app)
    scope = _ws_scope(host="evil.example.com", origin=b"http://test")
    await middleware(scope, receive, send)

    assert app_called is False
    assert events[0]["type"] == "websocket.close"


async def test_websocket_good_host_malformed_origin_rejected() -> None:
    """A valid Host with a garbage Origin must still be rejected, not waved
    through because it "isn't a mismatch we recognize"."""
    from partyboxd.api.security import HostOriginMiddleware

    app_called = False

    async def app(scope: object, receive: object, send: object) -> None:
        nonlocal app_called
        app_called = True

    events: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        events.append(message)

    async def receive() -> dict[str, object]:
        return {"type": "websocket.connect"}

    middleware = HostOriginMiddleware(app)
    scope = _ws_scope(origin=b"not a url")
    await middleware(scope, receive, send)

    assert app_called is False
    assert events[0]["type"] == "websocket.close"
