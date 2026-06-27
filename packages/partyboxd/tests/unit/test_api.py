"""Tests for the partyboxd HTTP API."""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock

from httpx import ASGITransport, AsyncClient
from partyboxd.api import create_app
from partyboxd.device.manager import StatusSnapshot


def _make_client(snapshot: StatusSnapshot) -> AsyncClient:
    manager = MagicMock()
    # snapshot is now a @property — use PropertyMock so the mock returns
    # the value on attribute access rather than on call.
    type(manager).snapshot = PropertyMock(return_value=snapshot)
    app = create_app(manager)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# GET /api/v1/status — disconnected
# ---------------------------------------------------------------------------


async def test_status_disconnected() -> None:
    snap = StatusSnapshot(connected=False, address=None, firmware=None, battery=None)
    async with _make_client(snap) as client:
        r = await client.get("/api/v1/status")
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is False
    assert body["address"] is None
    assert body["firmware"] is None
    assert body["battery"] is None
    assert "healthy" not in body


# ---------------------------------------------------------------------------
# GET /api/v1/status — connected, mains-powered
# ---------------------------------------------------------------------------


async def test_status_connected_no_battery() -> None:
    snap = StatusSnapshot(
        connected=True,
        address="AA:BB:CC:DD:EE:FF",
        firmware="26.2.10",
        battery=None,
    )
    async with _make_client(snap) as client:
        r = await client.get("/api/v1/status")
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is True
    assert body["address"] == "AA:BB:CC:DD:EE:FF"
    assert body["firmware"] == "26.2.10"
    assert body["battery"] is None


# ---------------------------------------------------------------------------
# GET /api/v1/status — connected, battery present
# ---------------------------------------------------------------------------


async def test_status_connected_with_battery() -> None:
    snap = StatusSnapshot(
        connected=True,
        address="AA:BB:CC:DD:EE:FF",
        firmware="26.2.10",
        battery=84,
    )
    async with _make_client(snap) as client:
        r = await client.get("/api/v1/status")
    assert r.status_code == 200
    assert r.json()["battery"] == 84


# ---------------------------------------------------------------------------
# 404 for unknown routes
# ---------------------------------------------------------------------------


async def test_unknown_route_returns_404() -> None:
    snap = StatusSnapshot(connected=False, address=None, firmware=None, battery=None)
    async with _make_client(snap) as client:
        r = await client.get("/api/v1/nonexistent")
    assert r.status_code == 404
