"""partybox-companion appliance entry point.

Starts the DeviceManager, the partyboxd HTTP API, the Companion Portal, and
the Spotify Connect service (librespot) as a single process. Both the daemon
and the Portal are served on the same port — the Portal at ``/`` and the
REST API at ``/api/v1/*``.

Usage::

    partybox-companion                                  # 0.0.0.0:8080
    COMPANION_PORT=80 partybox-companion                # bind to port 80
    COMPANION_SPOTIFY__CONNECT_NAME="Living Room" partybox-companion
    COMPANION_LOG_LEVEL=DEBUG partybox-companion        # verbose logging
"""

from __future__ import annotations

import asyncio
import logging
import logging.config
import os
from contextlib import suppress

import uvicorn
from partyboxd.api import create_app as create_daemon_app
from partyboxd.config import Settings as DaemonSettings
from partyboxd.device import DeviceManager
from partyboxd.device.events import SpeakerStateChangedEvent, VolumeChangedEvent

from companion.config import AudioSettings, CompanionSettings, SpotifySettings
from companion.config_store import ConfigStore
from companion.services.audio import AudioService
from companion.services.pairing import PairingService
from companion.services.provisioning import ProvisioningService
from companion.services.router import make_services_router
from companion.services.spotify import SpotifyService
from companion.supervisor import RestartPolicy, Supervisor
from companion.volume import VolumeState
from companion.webui.router import make_portal_router
from companion.wifi.middleware import CaptivePortalMiddleware
from companion.wifi.router import make_wifi_router

log = logging.getLogger(__name__)


def _make_log_config(level: str) -> dict[str, object]:
    # When stdout is connected to journald, it adds timestamps and priority.
    # Drop those from the Python format to avoid duplication.
    fmt = (
        "%(levelname)-8s %(name)s %(message)s"
        if "JOURNAL_STREAM" in os.environ
        else "%(asctime)s %(levelname)-8s %(name)s %(message)s"
    )
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": fmt,
                "datefmt": "%Y-%m-%dT%H:%M:%S",
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "default",
            }
        },
        "root": {"level": level, "handlers": ["console"]},
        "loggers": {
            "uvicorn.access": {"level": "WARNING"},
        },
    }


_AUDIO_GRACE_SECONDS = 300.0


async def _gate_spotify_on_audio(audio: AudioService, spotify: SpotifyService) -> None:
    """Gate SpotifyService on audio readiness.

    librespot advertises a Spotify Connect device over Zeroconf.  If it runs
    while A2DP is absent, Spotify clients can select "PartyBox" and receive
    silence.  This gate ensures Spotify is visible only when the
    appliance can actually produce audio.

    Subscribes to AudioService events; the BehaviorSubject-style subscribe()
    delivers the current state immediately, so the gate reaches the correct
    initial state without waiting for the next audio transition.

    Start: Spotify starts as soon as audio becomes ready.
    Stop: Spotify stops _AUDIO_GRACE_SECONDS after audio goes away, giving the
    A2DP link time to recover from transient drops before removing the Spotify
    Connect device from clients' device lists.

    SpotifyService.run() owns subprocess crash-recovery internally; this gate
    does not supervise it.  If spotify.run() ever exits other than by
    cancellation that is a violated invariant; let this coroutine propagate so
    the Supervisor can restart it.  See ADR-026.
    """
    queue = audio.subscribe()
    spotify_task: asyncio.Task[None] | None = None

    try:
        while True:
            event = await queue.get()

            if event.audio_ready:
                if spotify_task is None:
                    log.info("audio gate: audio ready — starting Spotify Connect")
                    spotify_task = asyncio.create_task(spotify.run(), name="spotify-service")
            else:
                if spotify_task is not None:
                    log.info(
                        "audio gate: audio unavailable — %.0fs grace before stopping Spotify",
                        _AUDIO_GRACE_SECONDS,
                    )
                    try:
                        recovery = await asyncio.wait_for(queue.get(), timeout=_AUDIO_GRACE_SECONDS)
                    except TimeoutError:
                        log.info("audio gate: grace period expired — stopping Spotify Connect")
                        spotify_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await spotify_task
                        spotify_task = None
                    else:
                        if recovery.audio_ready:
                            log.info("audio gate: audio restored within grace period")
    finally:
        audio.unsubscribe(queue)
        if spotify_task is not None:
            spotify_task.cancel()
            with suppress(asyncio.CancelledError):
                await spotify_task


async def _forward_ble_volume(manager: DeviceManager, volume_state: VolumeState) -> None:
    """Forward VolumeChangedEvents from the device bus into VolumeState.

    Subscribes to DeviceManager's event bus and updates VolumeState whenever
    the hardware reports a volume change.  Runs until cancelled.

    While BLE volume is not yet implemented this coroutine subscribes but
    never receives a VolumeChangedEvent.  It exists to establish the wiring
    before BLE notifications arrive; see ADR-022.
    """
    queue = manager.subscribe()
    try:
        while True:
            event = await queue.get()
            if isinstance(event, VolumeChangedEvent):
                volume_state.update(event.percent, "ble")
    finally:
        manager.unsubscribe(queue)


async def _recheck_audio_on_standby(manager: DeviceManager, audio: AudioService) -> None:
    """Nudge AudioService to re-check A2DP as soon as the speaker leaves "on".

    The BLE control link and the A2DP audio link are separate Bluetooth
    subsystems (see ``AudioService``'s module docstring), but in practice
    they tend to drop together — the speaker going into standby is a strong
    signal the audio link just went with it. Without this, the Portal's
    "Bluetooth Audio" status can show a stale "connected" for up to
    AudioService's 60s idle check interval after the speaker visibly went
    idle. Runs until cancelled.
    """
    queue = manager.subscribe()
    try:
        while True:
            event = await queue.get()
            if isinstance(event, SpeakerStateChangedEvent) and event.state != "on":
                audio.recheck_now()
    finally:
        manager.unsubscribe(queue)


def main() -> None:
    level = os.environ.get("COMPANION_LOG_LEVEL", "INFO").upper()
    logging.config.dictConfig(_make_log_config(level))
    daemon_settings = DaemonSettings()
    companion_settings = CompanionSettings()
    asyncio.run(_run(daemon_settings, companion_settings))


async def _run(
    daemon_settings: DaemonSettings,
    companion_settings: CompanionSettings,
) -> None:
    # Single ConfigStore shared across all routers — one file handle, one source of truth.
    config_store = ConfigStore(companion_settings.data_dir / "config.json")

    # Load portal config so user-saved settings (Spotify Connect name, bitrate)
    # survive reboots. The config file may not exist on first boot — defaults
    # are used.
    portal_cfg = config_store.read()
    effective_spotify = SpotifySettings(
        connect_name=portal_cfg.spotify_connect_name,
        bitrate=portal_cfg.spotify_bitrate,
        backend=companion_settings.spotify.backend,
    )
    volume_state = VolumeState()
    spotify = SpotifyService(effective_spotify, volume_state=volume_state)

    # A2DP address: prefer the persisted config value (set by first-time pairing)
    # over the env-var default so the Portal-saved address survives reboots.
    audio_sink = portal_cfg.audio_sink_address or companion_settings.audio.sink_address
    audio = AudioService(AudioSettings(sink_address=audio_sink))
    pairing = PairingService(config_store, audio)

    manager = DeviceManager(daemon_settings.speaker)

    provisioning = ProvisioningService(companion_settings.wifi.interface)

    app = create_daemon_app(
        manager,
        daemon_settings,
        audio_ready_fn=lambda: audio.audio_ready,
        extra_event_sources=[audio, spotify, pairing],
    )
    app.include_router(make_portal_router(companion_settings, config_store))
    app.include_router(
        make_services_router(
            spotify,
            config_store,
            manager=manager,
            volume_state=volume_state,
            audio=audio,
            pairing=pairing,
        )
    )
    app.include_router(make_wifi_router(provisioning))
    app.add_middleware(CaptivePortalMiddleware, provisioning=provisioning)

    server_config = uvicorn.Config(
        app,
        host=companion_settings.host,
        port=companion_settings.port,
        log_config=None,
    )
    server = uvicorn.Server(server_config)

    supervisor = Supervisor()
    supervisor.register("device-manager", manager.run)
    supervisor.register("spotify-audio-gate", lambda: _gate_spotify_on_audio(audio, spotify))
    supervisor.register("audio-service", audio.run)
    supervisor.register(
        "ble-volume-forwarder",
        lambda: _forward_ble_volume(manager, volume_state),
        policy=RestartPolicy(initial_delay=1.0, max_delay=30.0),
    )
    supervisor.register("provisioning", provisioning.run)
    supervisor.register(
        "audio-standby-recheck",
        lambda: _recheck_audio_on_standby(manager, audio),
        policy=RestartPolicy(initial_delay=1.0, max_delay=30.0),
    )

    supervisor_task = asyncio.create_task(supervisor.run(), name="supervisor")
    try:
        await server.serve()
    finally:
        supervisor_task.cancel()
        with suppress(asyncio.CancelledError):
            await supervisor_task


if __name__ == "__main__":
    main()
