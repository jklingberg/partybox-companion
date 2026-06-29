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
from partyboxd.device.events import VolumeChangedEvent

from companion.config import CompanionSettings, SpotifySettings
from companion.config_store import ConfigStore
from companion.services.audio import AudioService
from companion.services.provisioning import ProvisioningService
from companion.services.router import make_services_router
from companion.services.spotify import SpotifyService
from companion.volume import VolumeState
from companion.webui.router import make_portal_router
from companion.wifi.middleware import CaptivePortalMiddleware
from companion.wifi.router import make_wifi_router


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

    # Load portal config so user-saved settings (device name, bitrate) survive
    # reboots. The config file may not exist on first boot — defaults are used.
    portal_cfg = config_store.read()
    effective_spotify = SpotifySettings(
        connect_name=portal_cfg.spotify_connect_name,
        bitrate=portal_cfg.spotify_bitrate,
        backend=companion_settings.spotify.backend,
    )
    volume_state = VolumeState()
    spotify = SpotifyService(effective_spotify, volume_state=volume_state)
    audio = AudioService(companion_settings.audio)
    manager = DeviceManager(daemon_settings.speaker)

    provisioning = ProvisioningService(companion_settings.wifi.interface)

    app = create_daemon_app(manager, daemon_settings)
    app.include_router(make_portal_router(companion_settings, config_store))
    app.include_router(
        make_services_router(spotify, config_store, manager=manager, volume_state=volume_state)
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

    manager_task = asyncio.create_task(manager.run(), name="device-manager")
    spotify_task = asyncio.create_task(spotify.run(), name="spotify-service")
    audio_task = asyncio.create_task(audio.run(), name="audio-service")
    ble_volume_task = asyncio.create_task(
        _forward_ble_volume(manager, volume_state), name="ble-volume-forwarder"
    )
    provisioning_task = asyncio.create_task(provisioning.run(), name="provisioning")
    try:
        await server.serve()
    finally:
        provisioning_task.cancel()
        ble_volume_task.cancel()
        audio_task.cancel()
        spotify_task.cancel()
        manager_task.cancel()
        with suppress(asyncio.CancelledError):
            await provisioning_task
        with suppress(asyncio.CancelledError):
            await ble_volume_task
        with suppress(asyncio.CancelledError):
            await audio_task
        with suppress(asyncio.CancelledError):
            await spotify_task
        with suppress(asyncio.CancelledError):
            await manager_task


if __name__ == "__main__":
    main()
