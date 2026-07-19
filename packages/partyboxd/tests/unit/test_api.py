"""Tests for the partyboxd HTTP API."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest
from httpx import ASGITransport, AsyncClient
from partybox import BatteryStatusResponse, ChargingStatus
from partyboxd.api import create_app
from partyboxd.config import ApiSettings, Settings
from partyboxd.device.manager import DeviceNotConnectedError, StatusSnapshot


def _make_settings(api_key: str | None = None) -> Settings:
    return Settings(api=ApiSettings(api_key=api_key))


def _make_client(
    snapshot: StatusSnapshot,
    settings: Settings | None = None,
) -> AsyncClient:
    manager = MagicMock()
    type(manager).snapshot = PropertyMock(return_value=snapshot)
    manager.power_on = AsyncMock()
    manager.power_off = AsyncMock()
    app = create_app(manager, settings or _make_settings())
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


_CONNECTED = StatusSnapshot(
    connected=True,
    address="AA:BB:CC:DD:EE:FF",
    firmware="26.2.10",
    battery=None,
)
_DISCONNECTED = StatusSnapshot(connected=False, address=None, firmware=None, battery=None)
_WITH_BATTERY = StatusSnapshot(
    connected=True,
    address="AA:BB:CC:DD:EE:FF",
    firmware="26.2.10",
    battery=84,
    battery_status=BatteryStatusResponse(
        remaining_capacity_mah=4200,
        full_charge_capacity_mah=5000,
        charging_status=ChargingStatus.CHARGING,
        state_of_health_percent=99,
        cycle_count=1,
    ),
)


# ---------------------------------------------------------------------------
# GET /api/v1/health — unauthenticated, always 200
# ---------------------------------------------------------------------------


async def test_health_when_connected() -> None:
    async with _make_client(_CONNECTED) as client:
        r = await client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["ble_connected"] is True


async def test_health_when_disconnected() -> None:
    async with _make_client(_DISCONNECTED) as client:
        r = await client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["ble_connected"] is False


async def test_health_audio_ready_none_without_companion() -> None:
    """audio_ready is null when partyboxd runs standalone (no audio_ready_fn)."""
    async with _make_client(_CONNECTED) as client:
        r = await client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["audio_ready"] is None


async def test_health_audio_ready_true_when_fn_returns_true() -> None:
    """audio_ready reflects the audio_ready_fn result."""
    manager = MagicMock()
    type(manager).snapshot = PropertyMock(return_value=_CONNECTED)
    app = create_app(manager, _make_settings(), audio_ready_fn=lambda: True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["audio_ready"] is True


async def test_health_audio_ready_false_when_fn_returns_false() -> None:
    """audio_ready is false when audio_ready_fn returns False."""
    manager = MagicMock()
    type(manager).snapshot = PropertyMock(return_value=_CONNECTED)
    app = create_app(manager, _make_settings(), audio_ready_fn=lambda: False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["audio_ready"] is False


async def test_health_audio_focus_none_without_companion() -> None:
    """audio_focus is null when partyboxd runs standalone (no audio_focus_fn)."""
    async with _make_client(_CONNECTED) as client:
        r = await client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["audio_focus"] is None


async def test_health_audio_focus_reflects_fn() -> None:
    """audio_focus reflects the audio_focus_fn result."""
    manager = MagicMock()
    type(manager).snapshot = PropertyMock(return_value=_CONNECTED)
    app = create_app(manager, _make_settings(), audio_focus_fn=lambda: "contested")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["audio_focus"] == "contested"


async def test_health_requires_no_api_key() -> None:
    settings = _make_settings(api_key="secret")
    async with _make_client(_CONNECTED, settings) as client:
        r = await client.get("/api/v1/health")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/v1/speaker
# ---------------------------------------------------------------------------


async def test_speaker_connected() -> None:
    async with _make_client(_CONNECTED) as client:
        r = await client.get("/api/v1/speaker")
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is True
    assert body["address"] == "AA:BB:CC:DD:EE:FF"
    assert body["firmware"] == "26.2.10"
    assert body["battery"] is None


async def test_speaker_disconnected() -> None:
    async with _make_client(_DISCONNECTED) as client:
        r = await client.get("/api/v1/speaker")
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is False
    assert body["address"] is None
    assert body["firmware"] is None
    assert body["battery"] is None


async def test_speaker_with_battery() -> None:
    async with _make_client(_WITH_BATTERY) as client:
        r = await client.get("/api/v1/speaker")
    assert r.status_code == 200
    assert r.json()["battery"] == 84


# ---------------------------------------------------------------------------
# GET /api/v1/battery
# ---------------------------------------------------------------------------


async def test_battery_available() -> None:
    async with _make_client(_WITH_BATTERY) as client:
        r = await client.get("/api/v1/battery")
    assert r.status_code == 200
    assert r.json() == {
        "level": 84,
        "power_source": "mains",
        "charging": True,
        "remaining_playtime_minutes": None,
        "state_of_health_percent": 99,
        "cycle_count": 1,
    }


async def test_battery_not_available_returns_404() -> None:
    async with _make_client(_CONNECTED) as client:
        r = await client.get("/api/v1/battery")
    assert r.status_code == 404
    body = r.json()
    assert body["detail"]["error"] == "capability_unavailable"


async def test_battery_disconnected_returns_503() -> None:
    async with _make_client(_DISCONNECTED) as client:
        r = await client.get("/api/v1/battery")
    assert r.status_code == 503
    body = r.json()
    assert body["detail"]["error"] == "speaker_disconnected"


# ---------------------------------------------------------------------------
# POST /api/v1/power/on
# ---------------------------------------------------------------------------


async def test_power_on_success() -> None:
    manager = MagicMock()
    type(manager).snapshot = PropertyMock(return_value=_CONNECTED)
    manager.power_on = AsyncMock()
    app = create_app(manager, _make_settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/v1/power/on")
    assert r.status_code == 204
    manager.power_on.assert_awaited_once()


async def test_power_on_disconnected_returns_503() -> None:
    manager = MagicMock()
    type(manager).snapshot = PropertyMock(return_value=_DISCONNECTED)
    manager.power_on = AsyncMock(side_effect=DeviceNotConnectedError())
    app = create_app(manager, _make_settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/v1/power/on")
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "speaker_disconnected"


# ---------------------------------------------------------------------------
# POST /api/v1/power/off
# ---------------------------------------------------------------------------


async def test_power_off_success() -> None:
    manager = MagicMock()
    type(manager).snapshot = PropertyMock(return_value=_CONNECTED)
    manager.power_off = AsyncMock()
    app = create_app(manager, _make_settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/v1/power/off")
    assert r.status_code == 204
    manager.power_off.assert_awaited_once()


async def test_power_off_disconnected_returns_503() -> None:
    manager = MagicMock()
    type(manager).snapshot = PropertyMock(return_value=_DISCONNECTED)
    manager.power_off = AsyncMock(side_effect=DeviceNotConnectedError())
    app = create_app(manager, _make_settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/v1/power/off")
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "speaker_disconnected"


# ---------------------------------------------------------------------------
# POST /api/v1/bluetooth/reset
# ---------------------------------------------------------------------------


def _make_reset_client(result: str) -> AsyncClient:
    manager = MagicMock()
    type(manager).snapshot = PropertyMock(return_value=_DISCONNECTED)
    manager.request_adapter_reset = AsyncMock(return_value=result)
    app = create_app(manager, _make_settings())
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_bluetooth_reset_success() -> None:
    async with _make_reset_client("ok") as client:
        r = await client.post("/api/v1/bluetooth/reset")
    assert r.status_code == 204


async def test_bluetooth_reset_cooling_down_returns_429() -> None:
    async with _make_reset_client("cooling_down") as client:
        r = await client.post("/api/v1/bluetooth/reset")
    assert r.status_code == 429
    assert r.json()["detail"]["error"] == "adapter_reset_cooling_down"


async def test_bluetooth_reset_not_configured_returns_501() -> None:
    async with _make_reset_client("not_configured") as client:
        r = await client.post("/api/v1/bluetooth/reset")
    assert r.status_code == 501
    assert r.json()["detail"]["error"] == "adapter_reset_not_configured"


async def test_bluetooth_reset_failed_returns_502() -> None:
    async with _make_reset_client("failed") as client:
        r = await client.post("/api/v1/bluetooth/reset")
    assert r.status_code == 502
    assert r.json()["detail"]["error"] == "adapter_reset_failed"


async def test_bluetooth_reset_requires_auth() -> None:
    manager = MagicMock()
    type(manager).snapshot = PropertyMock(return_value=_DISCONNECTED)
    manager.request_adapter_reset = AsyncMock(return_value="ok")
    app = create_app(manager, _make_settings(api_key="secret"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/v1/bluetooth/reset")
    assert r.status_code == 401
    manager.request_adapter_reset.assert_not_awaited()


# ---------------------------------------------------------------------------
# API key authentication
# ---------------------------------------------------------------------------


async def test_auth_passes_with_correct_key() -> None:
    settings = _make_settings(api_key="secret")
    async with _make_client(_CONNECTED, settings) as client:
        r = await client.get("/api/v1/speaker", headers={"X-Api-Key": "secret"})
    assert r.status_code == 200


async def test_auth_rejected_with_wrong_key() -> None:
    settings = _make_settings(api_key="secret")
    async with _make_client(_CONNECTED, settings) as client:
        r = await client.get("/api/v1/speaker", headers={"X-Api-Key": "wrong"})
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "unauthorized"


async def test_auth_rejected_with_missing_key() -> None:
    settings = _make_settings(api_key="secret")
    async with _make_client(_CONNECTED, settings) as client:
        r = await client.get("/api/v1/speaker")
    assert r.status_code == 401


async def test_auth_disabled_when_no_key_configured() -> None:
    settings = _make_settings(api_key=None)
    async with _make_client(_CONNECTED, settings) as client:
        r = await client.get("/api/v1/speaker")
    assert r.status_code == 200


async def test_auth_applies_to_power_endpoints() -> None:
    settings = _make_settings(api_key="secret")
    manager = MagicMock()
    type(manager).snapshot = PropertyMock(return_value=_CONNECTED)
    manager.power_on = AsyncMock()
    app = create_app(manager, settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/v1/power/on")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 404 for unknown routes
# ---------------------------------------------------------------------------


async def test_unknown_route_returns_404() -> None:
    async with _make_client(_DISCONNECTED) as client:
        r = await client.get("/api/v1/nonexistent")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/v1/health — version field
# ---------------------------------------------------------------------------


async def test_health_includes_version() -> None:
    import partyboxd

    async with _make_client(_CONNECTED) as client:
        r = await client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["version"] == partyboxd.__version__


# ---------------------------------------------------------------------------
# Error shape — every domain error returns {detail: {error, message}}
# ---------------------------------------------------------------------------


def _make_power_client(exc: Exception, settings: Settings | None = None) -> AsyncClient:
    manager = MagicMock()
    type(manager).snapshot = PropertyMock(return_value=_DISCONNECTED)
    manager.power_on = AsyncMock(side_effect=exc)
    manager.power_off = AsyncMock(side_effect=exc)
    app = create_app(manager, settings or _make_settings())
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.parametrize(
    "method,path,client_factory",
    [
        # GET /battery — disconnected
        ("GET", "/api/v1/battery", lambda: _make_client(_DISCONNECTED)),
        # GET /battery — connected but no battery
        ("GET", "/api/v1/battery", lambda: _make_client(_CONNECTED)),
        # POST /power/on — speaker not connected
        (
            "POST",
            "/api/v1/power/on",
            lambda: _make_power_client(DeviceNotConnectedError()),
        ),
        # POST /power/off — speaker not connected
        (
            "POST",
            "/api/v1/power/off",
            lambda: _make_power_client(DeviceNotConnectedError()),
        ),
        # GET /speaker — wrong API key
        (
            "GET",
            "/api/v1/speaker",
            lambda: _make_client(_CONNECTED, _make_settings(api_key="x")),
        ),
    ],
)
async def test_error_shape(
    method: str,
    path: str,
    client_factory: object,
) -> None:
    """Every domain error must return {"detail": {"error": str, "message": str}}."""
    factory = client_factory  # type: ignore[assignment]
    client_ctx = factory()  # type: ignore[operator]
    async with client_ctx as client:
        r = await client.request(method, path)

    assert r.status_code >= 400
    body = r.json()
    assert "detail" in body, "missing 'detail' key"
    detail = body["detail"]
    assert isinstance(detail, dict), f"detail should be a dict, got {type(detail)}"
    assert "error" in detail, "missing 'error' key in detail"
    assert "message" in detail, "missing 'message' key in detail"
    assert isinstance(detail["error"], str)
    assert isinstance(detail["message"], str)


async def test_create_app_runs_provided_lifespan() -> None:
    """A lifespan passed to create_app must actually run — shutdown work the
    appliance registers there (supervisor cancellation, BLE disconnect) is
    the only cleanup that executes on a signal-initiated stop: uvicorn
    re-raises the captured signal the moment serve() returns, so code after
    serve() never runs."""
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    events: list[str] = []

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        events.append("startup")
        try:
            yield
        finally:
            events.append("shutdown")

    manager = MagicMock()
    app = create_app(manager, _make_settings(), lifespan=lifespan)

    async with app.router.lifespan_context(app):
        assert events == ["startup"]
    assert events == ["startup", "shutdown"]
