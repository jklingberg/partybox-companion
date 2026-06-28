"""REST endpoints for companion-managed services."""

from __future__ import annotations

import io
import json
import platform
import zipfile
from datetime import UTC, datetime

import partyboxd
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from companion.config import SpotifySettings
from companion.config_store import ConfigStore
from companion.services.spotify import SpotifyService


class SpotifyStatusResponse(BaseModel):
    """Response body for GET /api/v1/spotify."""

    running: bool
    active: bool
    device_name: str


def make_services_router(spotify: SpotifyService, config: ConfigStore) -> APIRouter:
    """Return an APIRouter with service-status and diagnostics endpoints.

    These endpoints are intentionally unauthenticated — they expose read-only
    appliance state and contain no sensitive data. The restart endpoint is also
    unauthenticated because it can only affect the local appliance and requires
    physical network access.
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

    # ------------------------------------------------------------------
    # POST /api/v1/spotify/restart — unauthenticated
    # ------------------------------------------------------------------

    @router.post(
        "/spotify/restart",
        status_code=204,
        summary="Restart Spotify Connect",
    )
    async def post_spotify_restart() -> None:
        """Restart the Spotify Connect service with the current saved configuration.

        Call this after saving new Spotify settings via ``PUT /api/v1/config``
        to apply them without rebooting the appliance.

        **Responses:**

        | Code | Meaning |
        |------|---------|
        | 204  | Restart initiated |
        """
        cfg = config.read()
        new_settings = SpotifySettings(
            connect_name=cfg.spotify_connect_name,
            bitrate=cfg.spotify_bitrate,
            backend=spotify.settings.backend,
        )
        spotify.update_settings(new_settings)

    # ------------------------------------------------------------------
    # GET /api/v1/debug/bundle — unauthenticated
    # ------------------------------------------------------------------

    @router.get(
        "/debug/bundle",
        summary="Download debug bundle",
        response_class=StreamingResponse,
    )
    async def get_debug_bundle() -> StreamingResponse:
        """Download a ZIP archive with diagnostic information.

        The bundle contains appliance version, configuration, service status,
        and system information useful for bug reports. No sensitive data
        (API keys, credentials) is included.

        **Responses:**

        | Code | Meaning |
        |------|---------|
        | 200  | ZIP archive returned |
        """
        cfg = config.read()
        spotify_status = spotify.status
        ts = datetime.now(tz=UTC)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "version.json",
                json.dumps(
                    {
                        "partyboxd": partyboxd.__version__,
                        "python": platform.python_version(),
                        "generated_at": ts.isoformat(),
                    },
                    indent=2,
                ),
            )
            zf.writestr("config.json", cfg.model_dump_json(indent=2))
            zf.writestr(
                "services.json",
                json.dumps(
                    {
                        "spotify": {
                            "running": spotify_status.running,
                            "active": spotify_status.active,
                            "device_name": spotify_status.device_name,
                        }
                    },
                    indent=2,
                ),
            )
            zf.writestr(
                "system.json",
                json.dumps(
                    {
                        "platform": platform.platform(),
                        "machine": platform.machine(),
                        "node": platform.node(),
                    },
                    indent=2,
                ),
            )

        buf.seek(0)
        filename = f"partybox-debug-{ts.strftime('%Y%m%d-%H%M%S')}.zip"
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return router
