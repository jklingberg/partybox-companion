"""REST endpoints for companion-managed services."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import platform
import zipfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Literal

import partyboxd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from partyboxd.device import DeviceManager
from partyboxd.device.manager import DeviceNotConnectedError, StatusSnapshot
from pydantic import BaseModel, Field

from companion.config import SpotifySettings
from companion.config_store import ConfigStore
from companion.services import pipewire_volume
from companion.services.audio import AudioService
from companion.services.pairing import PairingService, PairingState
from companion.services.spotify import SpotifyService
from companion.supervisor import Supervisor
from companion.volume import VolumeState

log = logging.getLogger(__name__)

_JOURNAL_LINES = 500


class AudioStatusResponse(BaseModel):
    """Response body for GET /api/v1/audio."""

    connected: bool
    address: str | None
    pairing_state: str
    error: str | None = None


class SpotifyStatusResponse(BaseModel):
    """Response body for GET /api/v1/spotify."""

    running: bool
    state: Literal["stopped", "playing", "paused"]
    device_name: str


class VolumeResponse(BaseModel):
    """Response body for GET /api/v1/volume."""

    level: int | None
    source: str | None


class VolumeBody(BaseModel):
    """Request body for POST /api/v1/volume."""

    level: Annotated[int, Field(ge=0, le=100)]


class TaskHealthResponse(BaseModel):
    """Serialised view of one supervised task, from ``Supervisor.health()``.

    ``last_exception`` is formatted as ``"<ExceptionType>: <message>"``, or
    ``None`` if the task has never failed or its last exit was a clean
    (unexpected) return. It is informational only — for a human reading the
    health sheet or a debug bundle — not a machine-readable or versioned
    format; callers must not parse it. Timestamps are omitted —
    ``TaskHealth``'s are ``time.monotonic()`` values with no cross-process
    meaning to a client.
    """

    name: str
    state: Literal["waiting", "running"]
    last_exception: str | None
    total_failures: int


class HealthDetailsResponse(BaseModel):
    """Response body for GET /api/v1/health/details."""

    tasks: list[TaskHealthResponse]


async def _collect_journal_logs() -> str:
    """Return recent journal entries for the companion unit, or a fallback message.

    Requires the service user to be able to read the system journal — the
    unit grants this via ``SupplementaryGroups=systemd-journal``.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "journalctl",
            "--unit=companion",
            f"--lines={_JOURNAL_LINES}",
            "--no-pager",
            "--output=short",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        output = stdout.decode(errors="replace")
        return output if output.strip() else "(no journal entries found)\n"
    except OSError, TimeoutError:
        return "(journalctl not available)\n"


def _device_snapshot_dict(snapshot: StatusSnapshot | None) -> dict[str, object]:
    """Serialise the daemon's device snapshot for the debug bundle.

    Includes the full battery reading (charging source, capacities, health)
    so a bundle captures exactly what the speaker reported — e.g. whether
    ``charging_status`` was present or ``null`` at collection time.
    """
    if snapshot is None:
        return {"available": False}
    bs = snapshot.battery_status
    battery: dict[str, object] | None = None
    if bs is not None:
        battery = {
            "charge_percent": bs.charge_percent,
            "charging_status": (
                bs.charging_status.name.lower() if bs.charging_status is not None else None
            ),
            "remaining_capacity_mah": bs.remaining_capacity_mah,
            "full_charge_capacity_mah": bs.full_charge_capacity_mah,
            "design_capacity_mah": bs.design_capacity_mah,
            "state_of_health_percent": bs.state_of_health_percent,
            "cycle_count": bs.cycle_count,
            "remaining_playtime_minutes": bs.remaining_playtime_minutes,
            "battery_id": bs.battery_id,
        }
    return {
        "available": True,
        "connected": snapshot.connected,
        "speaker_state": snapshot.speaker_state,
        "has_battery": snapshot.has_battery,
        "address": snapshot.address,
        "firmware": snapshot.firmware,
        "battery_percent": snapshot.battery,
        "battery_status": battery,
    }


def make_services_router(
    spotify: SpotifyService,
    config: ConfigStore,
    manager: DeviceManager | None = None,
    volume_state: VolumeState | None = None,
    audio: AudioService | None = None,
    pairing: PairingService | None = None,
    supervisor: Supervisor | None = None,
    auth: Callable[..., Awaitable[None]] | None = None,
    pipewire_get_volume: Callable[[], Awaitable[int | None]] = pipewire_volume.get_volume,
    pipewire_set_volume: Callable[[int], Awaitable[bool]] = pipewire_volume.set_volume,
) -> APIRouter:
    """Return an APIRouter with service-status and diagnostics endpoints.

    Most of these endpoints are intentionally unauthenticated — they expose
    read-only appliance state and contain no sensitive data. The restart
    endpoint is also unauthenticated because it can only affect the local
    appliance and requires physical network access.

    ``GET /api/v1/health/details`` is the exception: it exposes per-task crash
    detail (exception messages), so it requires *auth* — the same API-key
    dependency partyboxd's private routes use (``partyboxd.api.auth``) — when
    provided. See ADR-037.
    """
    router = APIRouter(prefix="/api/v1", tags=["services"])
    health_details_dependencies = [Depends(auth)] if auth is not None else []

    # ------------------------------------------------------------------
    # GET /api/v1/audio — unauthenticated
    # ------------------------------------------------------------------

    @router.get(
        "/audio",
        response_model=AudioStatusResponse,
        summary="Bluetooth audio status",
    )
    async def get_audio() -> AudioStatusResponse:
        """Current state of the Bluetooth A2DP audio connection.

        Always returns **200**.  ``connected`` is ``true`` when the Pi has an
        active A2DP link to the speaker.  ``address`` is the Classic Bluetooth
        MAC (``null`` when no pairing has been performed).  ``pairing_state``
        reflects any in-progress pairing attempt (``idle``, ``scanning``,
        ``pairing``, or ``failed``).

        **Responses:**

        | Code | Meaning |
        |------|---------|
        | 200  | Audio state returned |
        """
        a_status = audio.status if audio is not None else None
        p_status = pairing.status if pairing is not None else None
        return AudioStatusResponse(
            connected=a_status.connected if a_status else False,
            address=a_status.address if a_status else None,
            pairing_state=(p_status.state if p_status else PairingState.IDLE),
            error=p_status.error if p_status else None,
        )

    # ------------------------------------------------------------------
    # POST /api/v1/audio/pair — unauthenticated
    # ------------------------------------------------------------------

    @router.post(
        "/audio/pair",
        status_code=202,
        summary="Start Bluetooth pairing",
    )
    async def post_audio_pair() -> None:
        """Initiate a Bluetooth Classic pairing scan.

        Starts a background scan for a JBL speaker in pairing mode.  The
        caller must put the speaker into Bluetooth pairing mode **before**
        calling this endpoint.  Poll ``GET /api/v1/audio`` to track progress.

        Returns **202 Accepted** immediately; the scan runs for up to 60 s.
        Returns **409 Conflict** if a pairing attempt is already in progress.

        **Responses:**

        | Code | Meaning |
        |------|---------|
        | 202  | Pairing scan started |
        | 409  | Pairing already in progress |
        | 503  | Pairing service unavailable |
        """
        if pairing is None:
            raise HTTPException(
                status_code=503,
                detail={"error": "unavailable", "message": "Pairing service not available."},
            )
        started = await pairing.start()
        if not started:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "already_pairing",
                    "message": "A pairing attempt is already in progress.",
                },
            )

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
        running; ``state`` is librespot's actual playback state — one of
        ``"stopped"`` (no active session), ``"playing"``, or ``"paused"`` —
        reported via librespot's own event hook, not inferred from logs.

        **Responses:**

        | Code | Meaning |
        |------|---------|
        | 200  | Service state returned |
        """
        s = spotify.status
        return SpotifyStatusResponse(
            running=s.running,
            state=s.state,
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
    # GET /api/v1/health/details — authenticated
    # ------------------------------------------------------------------

    @router.get(
        "/health/details",
        response_model=HealthDetailsResponse,
        summary="Per-task supervisor health",
        dependencies=health_details_dependencies,
    )
    async def get_health_details() -> HealthDetailsResponse:
        """Per-task health detail for every task registered with the Supervisor.

        Powers the Portal's health sheet (docs/design/portal-redesign.md §4.4)
        with real per-task state instead of a synthesized view. ``state`` is
        ``"running"`` for a healthy task and ``"waiting"`` while it is backed
        off after an unexpected exit; ``last_exception`` and ``total_failures``
        describe the most recent and cumulative failures.

        Requires authentication (see ADR-037) — unlike the other endpoints in
        this router, it can reveal internal exception detail.

        **Responses:**

        | Code | Meaning |
        |------|---------|
        | 200 | Task health returned |
        | 401 | Missing or invalid API key |
        """
        tasks = supervisor.health() if supervisor is not None else []
        return HealthDetailsResponse(
            tasks=[
                TaskHealthResponse(
                    name=t.name,
                    state=t.state,
                    last_exception=(
                        f"{type(t.last_exception).__name__}: {t.last_exception}"
                        if t.last_exception is not None
                        else None
                    ),
                    total_failures=t.total_failures,
                )
                for t in tasks
            ]
        )

    # ------------------------------------------------------------------
    # POST /api/v1/factory-reset — unauthenticated
    # ------------------------------------------------------------------

    @router.post(
        "/factory-reset",
        status_code=204,
        summary="Factory reset the appliance",
    )
    async def post_factory_reset() -> None:
        """Return the appliance to its as-shipped state without a reboot.

        The contract — "indistinguishable from a freshly-flashed image" — and
        the rule for what future state must be added here are recorded in
        docs/adr/031-factory-reset-contract.md.

        Clears every piece of state that survives a power cycle, in order:

        1. Un-sets the live A2DP sink so the audio loop stops chasing the
           speaker whose bond is about to be removed.
        2. Removes the Bluetooth bond (``Adapter1.RemoveDevice``) so the
           speaker must be paired again.
        3. Deletes the saved configuration — Spotify name, bitrate,
           remembered speaker — reverting to factory defaults.
        4. Restarts Spotify Connect so its advertised name reflects the
           reset immediately.

        Bond removal is best-effort: a BlueZ failure is logged but does not
        abort the reset, so the configuration is always cleared. The appliance
        keeps running and returns to the un-paired state a fresh image starts
        in — no reboot required.

        **Responses:**

        | Code | Meaning |
        |------|---------|
        | 204  | Reset complete |
        """
        cfg = config.read()
        mac = cfg.audio_sink_address

        if audio is not None:
            audio.forget()

        if mac is not None and pairing is not None:
            try:
                await pairing.forget(mac)
            except Exception as exc:
                log.warning("Factory reset: bond removal for %s failed: %s", mac, exc)

        config.reset()

        defaults = SpotifySettings(backend=spotify.settings.backend)
        spotify.update_settings(defaults)
        log.info("Factory reset complete — appliance returned to defaults")

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
        audio_status = audio.status if audio is not None else None
        snapshot = manager.snapshot if manager is not None else None
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
                            "state": spotify_status.state,
                            "device_name": spotify_status.device_name,
                        },
                        "audio": {
                            "connected": audio_status.connected if audio_status else None,
                            "address": audio_status.address if audio_status else None,
                        },
                    },
                    indent=2,
                ),
            )
            zf.writestr(
                "device.json",
                json.dumps(_device_snapshot_dict(snapshot), indent=2),
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

        Tries the hardware (BLE) first; then the PipeWire sink actuator that
        actually drives audible output while BLE volume is unconfirmed (see
        ADR-022); finally falls back to the last known software volume if
        neither is available.

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
        if level is None:
            level = await pipewire_get_volume()
            if level is not None:
                source = "pipewire"
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

        Attempts to write volume to the speaker hardware via BLE first, then
        actuates via the PipeWire sink node — the mechanism that actually
        changes audible output while the BLE volume opcode is unconfirmed
        (see ADR-022). ``VolumeState.source`` records "pipewire" when that
        succeeded, or "api" as a last-resort optimistic write when neither
        actuator is available (e.g. audio not connected yet).

        **Responses:**

        | Code | Meaning |
        |------|---------|
        | 204  | Volume accepted |
        | 422  | Request body invalid (level out of range or wrong type) |
        """
        if manager is not None:
            try:
                await manager.set_volume(body.level)
            except DeviceNotConnectedError, NotImplementedError:
                pass
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail={"error": "invalid_level", "message": str(exc)},
                ) from exc
        actuated = await pipewire_set_volume(body.level)
        if volume_state is not None:
            volume_state.update(body.level, "pipewire" if actuated else "api")

    return router
