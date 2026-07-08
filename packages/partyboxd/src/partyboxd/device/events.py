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
class SpeakerStateChangedEvent:
    """Emitted when the speaker's coarse power state changes.

    ``state`` mirrors :attr:`StatusSnapshot.speaker_state` — ``"off"``
    (BLE disconnected), ``"standby"`` (BLE connected, speaker asleep), or
    ``"on"`` (BLE connected, speaker awake). Distinct from
    :class:`ConnectedEvent`/:class:`DisconnectedEvent`: those track the BLE
    link itself, while this tracks the on/standby transition that can happen
    *within* an established connection (see ADR/manager.py `_poll_battery`).
    """

    state: Literal["off", "standby", "on"]
    type: Literal["speaker_state_changed"] = "speaker_state_changed"


@dataclass(frozen=True)
class PowerChangedEvent:
    """Emitted after a successful power on/off command is accepted by the speaker."""

    state: Literal["on", "off"]
    type: Literal["power_changed"] = "power_changed"


@dataclass(frozen=True)
class VolumeChangedEvent:
    """Emitted when the speaker hardware reports a volume change.

    DeviceManager emits this event when a BLE volume notification arrives
    from the speaker (hardware button press, BLE SET command confirmation).
    It is not emitted for software-side volume changes (Spotify, REST API).

    ``percent`` is normalised to 0-100 before emission; the raw BLE value
    (0-65535) is converted inside the SDK.

    .. note::
        This event is defined now to establish the notification path before
        BLE volume is implemented.  DeviceManager does not emit it yet.
        See ADR-022 for the intended volume authority model.
    """

    percent: int
    type: Literal["volume_changed"] = "volume_changed"


DeviceEvent = (
    ConnectedEvent
    | DisconnectedEvent
    | SpeakerStateChangedEvent
    | PowerChangedEvent
    | VolumeChangedEvent
)


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
