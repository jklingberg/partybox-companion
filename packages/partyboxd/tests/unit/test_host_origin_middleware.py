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
