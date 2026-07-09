"""WebSocket event stream for partyboxd."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Sequence
from contextlib import suppress
from typing import Any, Protocol

from fastapi import APIRouter, Query, WebSocket

from partyboxd.config import Settings
from partyboxd.device import DeviceManager

log = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL = 20.0


class EventSource(Protocol):
    """Anything with subscribe()/unsubscribe() over dataclass-shaped events.

    Satisfied by :class:`~partyboxd.device.manager.DeviceManager` and by any
    :class:`~partyboxd.eventbus.EventBus`-backed companion service (see
    ``docs/adr/035-state-ownership-and-signal-pipeline.md``) — this lets
    companion fan its own events (audio/spotify/pairing) into the same
    WebSocket stream without partyboxd needing to know what they mean.
    """

    def subscribe(self) -> asyncio.Queue[Any]: ...
    def unsubscribe(self, queue: asyncio.Queue[Any]) -> None: ...


async def _forward(source: EventSource, sink: asyncio.Queue[Any]) -> None:
    """Relay every event from *source* into *sink* until cancelled."""
    queue = source.subscribe()
    try:
        while True:
            sink.put_nowait(await queue.get())
    finally:
        source.unsubscribe(queue)


def make_ws_router(
    manager: DeviceManager,
    settings: Settings,
    extra_sources: Sequence[EventSource] = (),
) -> APIRouter:
    """Return an APIRouter containing the WebSocket event-stream endpoint.

    *extra_sources* are additional event sources (companion's AudioService/
    SpotifyService/PairingService) fanned into the same stream as the
    DeviceManager events below — see ``docs/adr/035-state-ownership-and-signal-pipeline.md``.
    """
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
        | ``speaker_state_changed`` | ``state`` (off/standby/on) | Power state changed |
        | ``power_changed`` | ``state`` (``"on"`` or ``"off"``) | Power command accepted |
        | ``audio_changed`` | ``audio_ready``, ``address`` | A2DP link connected/dropped |
        | ``spotify_changed`` | ``running``, ``active``, ``device_name`` | Spotify state changed |
        | ``pairing_progress`` | ``state``, ``error`` | Pairing attempt progressed |
        | ``ping`` | — | Heartbeat sent every ~20 s when idle |

        The connection closes with code **4001** if the API key is invalid.
        """
        if expected_key is not None and api_key != expected_key:
            await websocket.close(code=4001)
            return

        await websocket.accept()
        log.debug("websocket client connected: %s", websocket.client)

        merged: asyncio.Queue[Any] = asyncio.Queue()
        sources: list[EventSource] = [manager, *extra_sources]
        forwarders = [asyncio.create_task(_forward(src, merged)) for src in sources]
        try:
            while True:
                try:
                    event = await asyncio.wait_for(merged.get(), timeout=_HEARTBEAT_INTERVAL)
                    await websocket.send_json(dataclasses.asdict(event))
                except TimeoutError:
                    await websocket.send_json({"type": "ping"})
        except Exception as exc:
            log.debug("websocket send error (client likely disconnected): %s", exc)
        finally:
            for task in forwarders:
                task.cancel()
            for task in forwarders:
                with suppress(asyncio.CancelledError):
                    await task
            log.debug("websocket client disconnected: %s", websocket.client)

    return router
