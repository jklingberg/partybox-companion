"""HTTP routes for the partyboxd REST API."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import partyboxd
from partyboxd.device import DeviceManager
from partyboxd.device.manager import DeviceNotConnectedError

# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    """Response body for GET /api/v1/health."""

    status: str
    version: str
    speaker_connected: bool


class SpeakerResponse(BaseModel):
    """Response body for GET /api/v1/speaker."""

    connected: bool
    address: str | None
    firmware: str | None
    battery: int | None


class BatteryResponse(BaseModel):
    """Response body for GET /api/v1/battery."""

    level: int


class VolumeResponse(BaseModel):
    """Response body for GET /api/v1/volume."""

    level: int | None


class VolumeRequest(BaseModel):
    """Request body for POST /api/v1/volume."""

    level: int


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


def _volume_not_implemented() -> HTTPException:
    return HTTPException(
        status_code=501,
        detail={
            "error": "not_implemented",
            "message": "Volume BLE opcode is not yet confirmed from hardware captures.",
        },
    )


def _volume_out_of_range() -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={
            "error": "invalid_request",
            "message": "Volume level must be between 0 and 100.",
        },
    )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def make_router(
    manager: DeviceManager,
    auth: Callable[..., Awaitable[None]],
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

        Always returns **200**. The ``speaker_connected`` field indicates
        whether the daemon currently has an active connection to the speaker.

        No authentication required — safe to poll from monitoring tools.

        **Responses:**

        | Code | Meaning |
        |------|---------|
        | 200 | Daemon is running |
        """
        return HealthResponse(
            status="ok",
            version=partyboxd.__version__,
            speaker_connected=manager.snapshot.connected,
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
        """Current battery level as a percentage (0-100).

        Only available on battery-powered models. Mains-powered speakers
        (e.g. PartyBox 520) return **404**.

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
        return BatteryResponse(level=snap.battery)

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
    # GET /api/v1/volume
    # ------------------------------------------------------------------

    @private.get(
        "/volume",
        response_model=VolumeResponse,
        tags=["volume"],
        summary="Current volume level",
    )
    async def get_volume() -> VolumeResponse:
        """Current speaker volume as a percentage (0-100).

        Returns ``{"level": null}`` when the BLE volume opcode is not yet
        implemented. Returns **503** when the speaker is not connected.

        **Responses:**

        | Code | Meaning |
        |------|---------|
        | 200 | Volume returned (``level`` may be ``null``) |
        | 401 | Missing or invalid API key |
        | 503 | Speaker is not connected |
        """
        try:
            level = await manager.get_volume()
        except DeviceNotConnectedError as exc:
            raise _speaker_disconnected() from exc
        return VolumeResponse(level=level)

    # ------------------------------------------------------------------
    # POST /api/v1/volume
    # ------------------------------------------------------------------

    @private.post(
        "/volume",
        status_code=204,
        tags=["volume"],
        summary="Set volume level",
    )
    async def post_volume(body: VolumeRequest) -> None:
        """Set the speaker volume to the given level (0-100).

        **Responses:**

        | Code | Meaning |
        |------|---------|
        | 204 | Volume set |
        | 400 | Level out of range |
        | 401 | Missing or invalid API key |
        | 501 | Volume BLE opcode not yet implemented |
        | 503 | Speaker is not connected |
        """
        try:
            await manager.set_volume(body.level)
        except ValueError as exc:
            raise _volume_out_of_range() from exc
        except NotImplementedError as exc:
            raise _volume_not_implemented() from exc
        except DeviceNotConnectedError as exc:
            raise _speaker_disconnected() from exc

    router = APIRouter()
    router.include_router(public)
    router.include_router(private)
    return router
