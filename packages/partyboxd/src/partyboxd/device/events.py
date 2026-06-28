"""Device events emitted by DeviceManager and delivered to API subscribers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

_QUEUE_MAX = 64


@dataclass(frozen=True)
class ConnectedEvent:
    """Emitted when the daemon establishes a connection with the speaker."""

    address: str | None
    firmware: str | None
    battery: int | None
    type: Literal["connected"] = "connected"


@dataclass(frozen=True)
class DisconnectedEvent:
    """Emitted when the speaker connection is lost or terminated."""

    type: Literal["disconnected"] = "disconnected"


@dataclass(frozen=True)
class PowerChangedEvent:
    """Emitted after a successful power on/off command is accepted by the speaker."""

    state: Literal["on", "off"]
    type: Literal["power_changed"] = "power_changed"


DeviceEvent = ConnectedEvent | DisconnectedEvent | PowerChangedEvent


class EventBus:
    """Broadcast dispatcher: one emitter, N async consumers.

    Each subscriber gets its own bounded queue. Slow consumers have events
    dropped silently rather than stalling the emitter.
    """

    def __init__(self) -> None:
        self._queues: list[asyncio.Queue[DeviceEvent]] = []

    def subscribe(self) -> asyncio.Queue[DeviceEvent]:
        """Return a queue that receives all future events until unsubscribed."""
        q: asyncio.Queue[DeviceEvent] = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._queues.append(q)
        return q

    def unsubscribe(self, queue: asyncio.Queue[DeviceEvent]) -> None:
        """Stop delivering events to *queue*."""
        try:
            self._queues.remove(queue)
        except ValueError:
            pass

    def emit(self, event: DeviceEvent) -> None:
        """Broadcast *event* to all current subscribers.

        Drops the event silently for any subscriber whose queue is full.
        """
        for q in self._queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass
