"""Tests for the volume REST API endpoints.

Covers GET /api/v1/volume and POST /api/v1/volume, including the software
fallback path via DeviceManager.volume_fallback.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest
from httpx import ASGITransport, AsyncClient
from partyboxd.api import create_app
from partyboxd.config import ApiSettings, Settings
from partyboxd.device.manager import DeviceNotConnectedError, StatusSnapshot


def _make_settings(api_key: str | None = None) -> Settings:
    return Settings(api=ApiSettings(api_key=api_key))


def _make_client(
    snapshot: StatusSnapshot,
    *,
    get_volume: int | None | Exception = None,
    set_volume_exc: Exception | None = None,
    settings: Settings | None = None,
) -> AsyncClient:
    manager = MagicMock()
    type(manager).snapshot = PropertyMock(return_value=snapshot)
    manager.power_on = AsyncMock()
    manager.power_off = AsyncMock()

    if isinstance(get_volume, Exception):
        manager.get_volume = AsyncMock(side_effect=get_volume)
    else:
        manager.get_volume = AsyncMock(return_value=get_volume)

    if set_volume_exc is not None:
        manager.set_volume = AsyncMock(side_effect=set_volume_exc)
    else:
        manager.set_volume = AsyncMock()

    app = create_app(manager, settings or _make_settings())
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


_CONNECTED = StatusSnapshot(
    connected=True,
    address="AA:BB:CC:DD:EE:FF",
    firmware="26.2.10",
    battery=None,
)
_DISCONNECTED = StatusSnapshot(connected=False, address=None, firmware=None, battery=None)


# ---------------------------------------------------------------------------
# GET /api/v1/volume
# ---------------------------------------------------------------------------


async def test_get_volume_returns_level_when_known() -> None:
    async with _make_client(_CONNECTED, get_volume=75) as client:
        r = await client.get("/api/v1/volume")
    assert r.status_code == 200
    assert r.json() == {"level": 75}


async def test_get_volume_returns_null_when_not_implemented() -> None:
    """When BLE opcode is not available, level is null (not an error)."""
    async with _make_client(_CONNECTED, get_volume=None) as client:
        r = await client.get("/api/v1/volume")
    assert r.status_code == 200
    assert r.json() == {"level": None}


async def test_get_volume_disconnected_returns_503() -> None:
    async with _make_client(_DISCONNECTED, get_volume=DeviceNotConnectedError()) as client:
        r = await client.get("/api/v1/volume")
    assert r.status_code == 503
    body = r.json()
    assert body["detail"]["error"] == "speaker_disconnected"


async def test_get_volume_returns_zero_percent() -> None:
    async with _make_client(_CONNECTED, get_volume=0) as client:
        r = await client.get("/api/v1/volume")
    assert r.status_code == 200
    assert r.json()["level"] == 0


async def test_get_volume_returns_100_percent() -> None:
    async with _make_client(_CONNECTED, get_volume=100) as client:
        r = await client.get("/api/v1/volume")
    assert r.status_code == 200
    assert r.json()["level"] == 100


# ---------------------------------------------------------------------------
# POST /api/v1/volume
# ---------------------------------------------------------------------------


async def test_post_volume_success_returns_204() -> None:
    async with _make_client(_CONNECTED) as client:
        r = await client.post("/api/v1/volume", json={"level": 50})
    assert r.status_code == 204


async def test_post_volume_disconnected_returns_503() -> None:
    async with _make_client(_DISCONNECTED, set_volume_exc=DeviceNotConnectedError()) as client:
        r = await client.post("/api/v1/volume", json={"level": 50})
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "speaker_disconnected"


async def test_post_volume_out_of_range_returns_400() -> None:
    async with _make_client(_CONNECTED, set_volume_exc=ValueError("out of range")) as client:
        r = await client.post("/api/v1/volume", json={"level": 200})
    assert r.status_code == 400
    body = r.json()
    assert body["detail"]["error"] == "invalid_request"


async def test_post_volume_not_implemented_returns_501() -> None:
    async with _make_client(
        _CONNECTED, set_volume_exc=NotImplementedError("BLE opcode TBD")
    ) as client:
        r = await client.post("/api/v1/volume", json={"level": 50})
    assert r.status_code == 501
    body = r.json()
    assert body["detail"]["error"] == "not_implemented"


async def test_post_volume_boundary_0() -> None:
    async with _make_client(_CONNECTED) as client:
        r = await client.post("/api/v1/volume", json={"level": 0})
    assert r.status_code == 204


async def test_post_volume_boundary_100() -> None:
    async with _make_client(_CONNECTED) as client:
        r = await client.post("/api/v1/volume", json={"level": 100})
    assert r.status_code == 204


# ---------------------------------------------------------------------------
# Error shape — detail must be {error, message}
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        # GET /volume — disconnected
        (
            "GET",
            "/api/v1/volume",
            {"get_volume": DeviceNotConnectedError()},
        ),
        # POST /volume — not implemented
        (
            "POST",
            "/api/v1/volume",
            {"set_volume_exc": NotImplementedError("TBD")},
        ),
        # POST /volume — out of range
        (
            "POST",
            "/api/v1/volume",
            {"set_volume_exc": ValueError("bad")},
        ),
    ],
)
async def test_volume_error_shape(
    method: str,
    path: str,
    kwargs: dict[str, object],
) -> None:
    """All volume errors must return {"detail": {"error": str, "message": str}}."""
    snapshot = _DISCONNECTED if method == "GET" else _CONNECTED
    client_ctx = _make_client(snapshot, **kwargs)  # type: ignore[arg-type]
    body_json = {"level": 50} if method == "POST" else None
    async with client_ctx as client:
        r = await client.request(method, path, json=body_json)

    assert r.status_code >= 400
    body = r.json()
    assert "detail" in body
    detail = body["detail"]
    assert isinstance(detail, dict)
    assert "error" in detail
    assert "message" in detail
