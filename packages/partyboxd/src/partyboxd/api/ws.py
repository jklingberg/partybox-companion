"""WebSocket event stream for partyboxd."""

from __future__ import annotations

import asyncio
import dataclasses
import logging

from fastapi import APIRouter, Query, WebSocket

from partyboxd.config import Settings
from partyboxd.device import DeviceManager

log = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL = 20.0


def make_ws_router(manager: DeviceManager, settings: Settings) -> APIRouter:
    """Return an APIRouter containing the WebSocket event-stream endpoint."""
    router = APIRouter(prefix="/api/v1")
    expected_key = settings.api.api_key

    @router.websocket("/events")
    async def ws_events(
        websocket: WebSocket,
        api_key: str | None = Query(default=None),
    ) -> None:
        """Real-time device event stream.

        Authenticate by passing the API key as the ``api_key`` query parameter::

            ws://host:port/api/v1/events?api_key=<key>

        When API key authentication is disabled (default), the parameter is
        ignored.

        **Events** are JSON objects with a ``type`` discriminator field:

        | ``type`` | Fields | When |
        |----------|--------|------|
        | ``connected`` | ``address``, ``firmware``, ``battery`` | Speaker connected |
        | ``disconnected`` | — | Speaker disconnected |
        | ``power_changed`` | ``state`` (``"on"`` or ``"off"``) | Power command accepted |
        | ``ping`` | — | Heartbeat sent every ~20 s when idle |

        The connection closes with code **4001** if the API key is invalid.
        """
        if expected_key is not None and api_key != expected_key:
            await websocket.close(code=4001)
            return

        await websocket.accept()
        log.debug("websocket client connected: %s", websocket.client)

        queue = manager.subscribe()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_INTERVAL)
                    await websocket.send_json(dataclasses.asdict(event))
                except TimeoutError:
                    await websocket.send_json({"type": "ping"})
        except Exception as exc:
            log.debug("websocket send error (client likely disconnected): %s", exc)
        finally:
            manager.unsubscribe(queue)
            log.debug("websocket client disconnected: %s", websocket.client)

    return router
