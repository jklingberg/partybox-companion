"""REST endpoints for companion-managed services."""

from __future__ import annotations

import asyncio
import io
import json
import platform
import zipfile
from datetime import UTC, datetime
from typing import Annotated

import partyboxd
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from partyboxd.device import DeviceManager
from partyboxd.device.manager import DeviceNotConnectedError
from pydantic import BaseModel, Field

from companion.config import SpotifySettings
from companion.config_store import ConfigStore
from companion.services.spotify import SpotifyService
from companion.volume import VolumeState

_JOURNAL_LINES = 500


class SpotifyStatusResponse(BaseModel):
    """Response body for GET /api/v1/spotify."""

    running: bool
    active: bool
    device_name: str


class VolumeResponse(BaseModel):
    """Response body for GET /api/v1/volume."""

    level: int | None
    source: str | None


class VolumeBody(BaseModel):
    """Request body for POST /api/v1/volume."""

    level: Annotated[int, Field(ge=0, le=100)]


async def _collect_journal_logs() -> str:
    """Return recent journal entries for partybox-companion, or a fallback message."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "journalctl",
            "--unit=partybox-companion",
            f"--lines={_JOURNAL_LINES}",
            "--no-pager",
            "--output=short",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        output = stdout.decode(errors="replace")
        return output if output.strip() else "(no journal entries found)\n"
    except (OSError, TimeoutError):
        return "(journalctl not available)\n"


def make_services_router(
    spotify: SpotifyService,
    config: ConfigStore,
    manager: DeviceManager | None = None,
    volume_state: VolumeState | None = None,
) -> APIRouter:
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
        system information, and recent log lines. No sensitive data
        (API keys, credentials) is included.

        **Responses:**

        | Code | Meaning |
        |------|---------|
        | 200  | ZIP archive returned |
        """
        cfg = config.read()
        spotify_status = spotify.status
        ts = datetime.now(tz=UTC)
        logs = await _collect_journal_logs()

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
            zf.writestr("logs.txt", logs)

        buf.seek(0)
        filename = f"partybox-debug-{ts.strftime('%Y%m%d-%H%M%S')}.zip"
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # ------------------------------------------------------------------
    # GET /api/v1/volume — unauthenticated
    # ------------------------------------------------------------------

    @router.get(
        "/volume",
        response_model=VolumeResponse,
        summary="Logical speaker volume",
    )
    async def get_volume() -> VolumeResponse:
        """Current logical speaker volume (0-100).

        Tries the hardware (BLE) first; falls back to the last known
        software volume when the speaker is disconnected or the BLE opcode
        is not yet confirmed.

        **Responses:**

        | Code | Meaning |
        |------|---------|
        | 200  | Volume returned (``level`` is ``null`` if unknown) |
        """
        level: int | None = None
        source: str | None = None
        if manager is not None:
            try:
                level = await manager.get_volume()
                if level is not None:
                    source = "ble"
            except DeviceNotConnectedError:
                pass
        if level is None and volume_state is not None:
            level = volume_state.level
            source = volume_state.source
        return VolumeResponse(level=level, source=source)

    # ------------------------------------------------------------------
    # POST /api/v1/volume — unauthenticated
    # ------------------------------------------------------------------

    @router.post(
        "/volume",
        status_code=204,
        summary="Set logical speaker volume",
    )
    async def post_volume(body: VolumeBody) -> None:
        """Set the logical speaker volume (0-100).

        Attempts to write volume to the speaker hardware via BLE. While the
        BLE volume opcode is not yet confirmed, the value is stored in the
        appliance's in-memory volume state and reflected by GET /api/v1/volume.

        **Responses:**

        | Code | Meaning |
        |------|---------|
        | 204  | Volume accepted |
        | 422  | Request body invalid (level out of range or wrong type) |
        """
        if manager is not None:
            try:
                await manager.set_volume(body.level)
            except (DeviceNotConnectedError, NotImplementedError):
                pass
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail={"error": "invalid_level", "message": str(exc)},
                ) from exc
        if volume_state is not None:
            volume_state.update(body.level, "api")

    return router
