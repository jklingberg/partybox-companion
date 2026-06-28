"""partybox-companion appliance entry point.

Starts the DeviceManager, the partyboxd HTTP API, the Companion Portal, and
the Spotify Connect service (librespot) as a single process. Both the daemon
and the Portal are served on the same port — the Portal at ``/`` and the
REST API at ``/api/v1/*``.

Usage::

    partybox-companion                                  # 0.0.0.0:8080
    COMPANION_PORT=80 partybox-companion                # bind to port 80
    COMPANION_SPOTIFY__CONNECT_NAME="Living Room" partybox-companion
"""

from __future__ import annotations

import asyncio
import logging
import logging.config
from contextlib import suppress

import uvicorn
from partyboxd.api import create_app as create_daemon_app
from partyboxd.config import Settings as DaemonSettings
from partyboxd.device import DeviceManager

from companion.config import CompanionSettings
from companion.services.audio import AudioService
from companion.services.router import make_services_router
from companion.services.spotify import SpotifyService
from companion.webui.router import make_portal_router

_LOG_CONFIG: dict[str, object] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s %(levelname)-8s %(name)s %(message)s",
            "datefmt": "%Y-%m-%dT%H:%M:%S",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "default",
        }
    },
    "root": {"level": "INFO", "handlers": ["console"]},
    "loggers": {
        "uvicorn.access": {"level": "WARNING"},
    },
}


def main() -> None:
    logging.config.dictConfig(_LOG_CONFIG)
    daemon_settings = DaemonSettings()
    companion_settings = CompanionSettings()
    asyncio.run(_run(daemon_settings, companion_settings))


async def _run(
    daemon_settings: DaemonSettings,
    companion_settings: CompanionSettings,
) -> None:
    manager = DeviceManager(daemon_settings.speaker)
    spotify = SpotifyService(companion_settings.spotify)
    audio = AudioService(companion_settings.audio)

    app = create_daemon_app(manager, daemon_settings)
    app.include_router(make_portal_router(companion_settings))
    app.include_router(make_services_router(spotify))

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
    try:
        await server.serve()
    finally:
        audio_task.cancel()
        spotify_task.cancel()
        manager_task.cancel()
        with suppress(asyncio.CancelledError):
            await audio_task
        with suppress(asyncio.CancelledError):
            await spotify_task
        with suppress(asyncio.CancelledError):
            await manager_task


if __name__ == "__main__":
    main()
