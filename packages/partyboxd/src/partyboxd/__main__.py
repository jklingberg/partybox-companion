"""partyboxd daemon entry point.

Starts the DeviceManager (connection lifecycle) and the HTTP server
(status API) as concurrent asyncio tasks. Both run until SIGINT/SIGTERM,
then shut down cleanly.

Usage::

    partyboxd                         # defaults: host=127.0.0.1, port=8765
    PARTYBOXD_SERVER__PORT=9000 partyboxd
"""

from __future__ import annotations

import asyncio
import logging
import logging.config
from contextlib import suppress

import uvicorn

from partyboxd.api import create_app
from partyboxd.config import Settings
from partyboxd.device import DeviceManager

_LOG_CONFIG = {
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
    # Silence uvicorn's access log — the status endpoint is polled often.
    "loggers": {
        "uvicorn.access": {"level": "WARNING"},
    },
}


def main() -> None:
    logging.config.dictConfig(_LOG_CONFIG)
    settings = Settings()
    asyncio.run(_run(settings))


async def _run(settings: Settings) -> None:
    manager = DeviceManager(settings.speaker)
    app = create_app(manager, settings)

    server_config = uvicorn.Config(
        app,
        host=settings.server.host,
        port=settings.server.port,
        log_config=None,  # we configure logging ourselves
    )
    server = uvicorn.Server(server_config)

    manager_task = asyncio.create_task(manager.run(), name="device-manager")
    try:
        await server.serve()
    finally:
        manager_task.cancel()
        with suppress(asyncio.CancelledError):
            await manager_task


if __name__ == "__main__":
    main()
