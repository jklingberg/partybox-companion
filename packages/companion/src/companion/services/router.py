"""REST endpoints for companion-managed services."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from companion.services.spotify import SpotifyService


class SpotifyStatusResponse(BaseModel):
    """Response body for GET /api/v1/spotify."""

    running: bool
    active: bool
    device_name: str


def make_services_router(spotify: SpotifyService) -> APIRouter:
    """Return an APIRouter with service-status endpoints.

    These endpoints are intentionally unauthenticated — they expose read-only
    appliance state (is Spotify running, is playback active) and contain no
    sensitive data.
    """
    router = APIRouter(prefix="/api/v1", tags=["services"])

    # ------------------------------------------------------------------
    # GET /api/v1/spotify — unauthenticated
    # ------------------------------------------------------------------

    @router.get(
        "/spotify",
        response_model=SpotifyStatusResponse,
        summary="Spotify Connect status",
    )
    async def get_spotify() -> SpotifyStatusResponse:
        """Current state of the Spotify Connect service.

        Always returns **200**. ``running`` indicates whether librespot is
        running; ``active`` indicates whether Spotify playback is currently
        in progress.

        No authentication required — status only, no sensitive data.

        **Responses:**

        | Code | Meaning |
        |------|---------|
        | 200  | Service state returned |
        """
        s = spotify.status
        return SpotifyStatusResponse(
            running=s.running,
            active=s.active,
            device_name=s.device_name,
        )

    return router
