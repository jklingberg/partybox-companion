"""HTTP routes for the partyboxd REST API."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from partybox import ChargingStatus
from pydantic import BaseModel

import partyboxd
from partyboxd.device import DeviceManager
from partyboxd.device.manager import DeviceNotConnectedError, StatusSnapshot

# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    """Response body for GET /api/v1/health."""

    status: str
    version: str
    ble_connected: bool
    audio_ready: bool | None = None
    speaker_state: Literal["off", "unreachable", "standby", "on"]
    #: "exclusive" | "contested" | "unknown" — whether another Bluetooth
    #: source is connected to the speaker (see companion's AudioFocusService).
    #: ``None`` when running as standalone partyboxd without Companion.
    audio_focus: str | None = None


class SpeakerResponse(BaseModel):
    """Response body for GET /api/v1/speaker."""

    connected: bool
    address: str | None
    firmware: str | None
    battery: int | None


class BatteryResponse(BaseModel):
    """Response body for GET /api/v1/battery.

    ``level`` is the derived charge percentage. The remaining fields come from
    the speaker's vendor battery report and are ``None`` if unavailable.
    """

    level: int
    power_source: Literal["mains", "battery"] | None = None
    charging: bool | None = None
    remaining_playtime_minutes: int | None = None
    state_of_health_percent: int | None = None
    cycle_count: int | None = None


def _battery_response(snap: StatusSnapshot) -> BatteryResponse:
    """Build the battery response from the daemon snapshot."""
    status = snap.battery_status
    source: Literal["mains", "battery"] | None = None
    charging: bool | None = None
    if status is not None and status.charging_status is not None:
        cs = status.charging_status
        source = "mains" if cs.on_mains else "battery"
        charging = cs is ChargingStatus.CHARGING
    return BatteryResponse(
        level=snap.battery if snap.battery is not None else 0,
        power_source=source,
        charging=charging,
        remaining_playtime_minutes=status.remaining_playtime_minutes if status else None,
        state_of_health_percent=status.state_of_health_percent if status else None,
        cycle_count=status.cycle_count if status else None,
    )


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------


def _speaker_disconnected() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "error": "speaker_disconnected",
            "message": "Speaker is not currently connected.",
        },
    )


def _capability_unavailable() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "error": "capability_unavailable",
            "message": "This speaker does not have a battery.",
        },
    )


def _adapter_reset_not_configured() -> HTTPException:
    return HTTPException(
        status_code=501,
        detail={
            "error": "adapter_reset_not_configured",
            "message": "Bluetooth adapter reset is not available on this deployment.",
        },
    )


def _adapter_reset_cooling_down() -> HTTPException:
    return HTTPException(
        status_code=429,
        detail={
            "error": "adapter_reset_cooling_down",
            "message": "A Bluetooth adapter reset was requested too recently. Try again shortly.",
        },
    )


def _adapter_reset_failed() -> HTTPException:
    return HTTPException(
        status_code=502,
        detail={
            "error": "adapter_reset_failed",
            "message": "Bluetooth adapter reset did not complete successfully.",
        },
    )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def make_router(
    manager: DeviceManager,
    auth: Callable[..., Awaitable[None]],
    audio_ready_fn: Callable[[], bool] | None = None,
    audio_focus_fn: Callable[[], str] | None = None,
) -> APIRouter:
    """Return an APIRouter with all partyboxd routes bound to *manager*.

    *auth* is a FastAPI dependency that enforces API key authentication on
    all routes except ``GET /health``, which is always publicly accessible.
    """

    public = APIRouter(prefix="/api/v1", tags=["health"])
    private = APIRouter(prefix="/api/v1", dependencies=[Depends(auth)])

    # ------------------------------------------------------------------
    # GET /api/v1/health — unauthenticated
    # ------------------------------------------------------------------

    @public.get(
        "/health",
        response_model=HealthResponse,
        summary="Daemon health",
    )
    async def get_health() -> HealthResponse:
        """Daemon liveness check.

        Always returns **200**.  ``ble_connected`` indicates whether the daemon
        has an active BLE GATT connection to the speaker.  ``audio_ready``
        indicates whether the appliance can currently produce audio (A2DP
        connected); ``null`` when running as standalone partyboxd without
        Companion. ``speaker_state`` is the coarse power state derived from
        the connection and battery signal: ``"off"`` (BLE disconnected —
        speaker unplugged or unreachable), ``"standby"`` (BLE connected but
        the speaker is asleep — only detectable on battery-capable models),
        or ``"on"``. ``audio_focus`` reports whether another Bluetooth source
        (typically a phone) is also connected to the speaker — ``"exclusive"``,
        ``"contested"``, or ``"unknown"``; ``null`` without Companion. A
        ``"contested"`` value explains the "everything connected but silent"
        failure mode: the speaker may render the other source instead.

        No authentication required — safe to poll from monitoring tools.

        **Responses:**

        | Code | Meaning |
        |------|---------|
        | 200 | Daemon is running |
        """
        return HealthResponse(
            status="ok",
            version=partyboxd.__version__,
            ble_connected=manager.snapshot.connected,
            audio_ready=audio_ready_fn() if audio_ready_fn is not None else None,
            speaker_state=manager.snapshot.speaker_state,
            audio_focus=audio_focus_fn() if audio_focus_fn is not None else None,
        )

    # ------------------------------------------------------------------
    # GET /api/v1/speaker
    # ------------------------------------------------------------------

    @private.get(
        "/speaker",
        response_model=SpeakerResponse,
        tags=["speaker"],
        summary="Speaker state",
    )
    async def get_speaker() -> SpeakerResponse:
        """Current speaker connection state.

        Always returns **200**. When the speaker is not connected,
        ``connected`` is ``false`` and all other fields are ``null``.

        **Responses:**

        | Code | Meaning |
        |------|---------|
        | 200 | Speaker state returned |
        | 401 | Missing or invalid API key |
        """
        snap = manager.snapshot
        return SpeakerResponse(
            connected=snap.connected,
            address=snap.address,
            firmware=snap.firmware,
            battery=snap.battery,
        )

    # ------------------------------------------------------------------
    # GET /api/v1/battery
    # ------------------------------------------------------------------

    @private.get(
        "/battery",
        response_model=BatteryResponse,
        tags=["speaker"],
        summary="Battery level",
    )
    async def get_battery() -> BatteryResponse:
        """Current battery status.

        ``level`` is the charge percentage (0-100), derived from the speaker's
        reported capacities. The response also reports the power source
        (``power_source``/``charging``), remaining playtime, state of health and
        charge-cycle count when the speaker provides them. Available on models
        with an internal battery (including the PartyBox 520); speakers with no
        battery return **404**.

        **Responses:**

        | Code | Meaning |
        |------|---------|
        | 200 | Battery level returned |
        | 401 | Missing or invalid API key |
        | 404 | Speaker does not have a battery |
        | 503 | Speaker is not connected |
        """
        snap = manager.snapshot
        if not snap.connected:
            raise _speaker_disconnected()
        if snap.battery is None:
            raise _capability_unavailable()
        return _battery_response(snap)

    # ------------------------------------------------------------------
    # POST /api/v1/power/on
    # ------------------------------------------------------------------

    @private.post(
        "/power/on",
        status_code=204,
        tags=["power"],
        summary="Turn speaker on",
    )
    async def post_power_on() -> None:
        """Send a power-on command to the speaker.

        **Responses:**

        | Code | Meaning |
        |------|---------|
        | 204 | Command accepted |
        | 401 | Missing or invalid API key |
        | 503 | Speaker is not connected |
        """
        try:
            await manager.power_on()
        except DeviceNotConnectedError as exc:
            raise _speaker_disconnected() from exc

    # ------------------------------------------------------------------
    # POST /api/v1/power/off
    # ------------------------------------------------------------------

    @private.post(
        "/power/off",
        status_code=204,
        tags=["power"],
        summary="Turn speaker off",
    )
    async def post_power_off() -> None:
        """Send a power-off command to the speaker.

        **Responses:**

        | Code | Meaning |
        |------|---------|
        | 204 | Command accepted |
        | 401 | Missing or invalid API key |
        | 503 | Speaker is not connected |
        """
        try:
            await manager.power_off()
        except DeviceNotConnectedError as exc:
            raise _speaker_disconnected() from exc

    # ------------------------------------------------------------------
    # POST /api/v1/bluetooth/reset
    # ------------------------------------------------------------------

    @private.post(
        "/bluetooth/reset",
        status_code=204,
        tags=["speaker"],
        summary="Manually power-cycle the Bluetooth adapter",
    )
    async def post_bluetooth_reset() -> None:
        """Power-cycle the appliance's Bluetooth adapter on demand.

        Intended for the ``"unreachable"`` state — BLE control down but the
        speaker's beacon still seen — where ADR-039's automatic wedge
        detection may not have triggered (it only catches *dense* connect
        failure bursts, not slow/intermittent ones). Drops any live A2DP
        audio too; use as a manual last resort, not routinely.

        Rate-limited independently of the automatic recovery path — see
        ``DeviceManager.request_adapter_reset``.

        **Responses:**

        | Code | Meaning |
        |------|---------|
        | 204 | Adapter reset completed |
        | 401 | Missing or invalid API key |
        | 429 | Reset requested too recently; try again shortly |
        | 501 | Not available on this deployment (bare partyboxd, no Companion) |
        | 502 | Reset was attempted but did not complete successfully |
        """
        result = await manager.request_adapter_reset()
        if result == "not_configured":
            raise _adapter_reset_not_configured()
        if result == "cooling_down":
            raise _adapter_reset_cooling_down()
        if result == "failed":
            raise _adapter_reset_failed()

    router = APIRouter()
    router.include_router(public)
    router.include_router(private)
    return router
