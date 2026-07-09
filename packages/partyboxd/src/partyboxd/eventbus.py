"""Generic in-process publish/subscribe event bus.

Broadcast dispatcher: one emitter, N async consumers, each with its own
bounded queue so a slow consumer never blocks the emitter or other
subscribers. Used by :class:`~partyboxd.device.manager.DeviceManager` and,
one layer up, by companion's ``AudioService``/``SpotifyService``/
``PairingService`` — see
``docs/adr/035-state-ownership-and-signal-pipeline.md`` for how these
compose into the Portal's WebSocket event stream.
"""

from __future__ import annotations

import asyncio

_DEFAULT_QUEUE_MAX = 64


class EventBus[T]:
    """Broadcasts events of type ``T`` to any number of subscribers."""

    def __init__(self, *, queue_max: int = _DEFAULT_QUEUE_MAX) -> None:
        self._queue_max = queue_max
        self._queues: list[asyncio.Queue[T]] = []

    def subscribe(self) -> asyncio.Queue[T]:
        """Return a queue that receives all future events until unsubscribed."""
        q: asyncio.Queue[T] = asyncio.Queue(maxsize=self._queue_max)
        self._queues.append(q)
        return q

    def unsubscribe(self, queue: asyncio.Queue[T]) -> None:
        """Stop delivering events to *queue*."""
        try:
            self._queues.remove(queue)
        except ValueError:
            pass

    def emit(self, event: T) -> None:
        """Broadcast *event* to all current subscribers.

        Drops the event silently for any subscriber whose queue is full.
        """
        for q in self._queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass
