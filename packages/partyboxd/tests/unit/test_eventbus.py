"""Unit tests for the generic EventBus.

Shared by DeviceManager and, one layer up, companion's AudioService/
SpotifyService/PairingService — see
docs/adr/035-state-ownership-and-signal-pipeline.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from partyboxd.eventbus import EventBus


@dataclass(frozen=True)
class _Event:
    value: str


def test_subscribe_returns_empty_queue() -> None:
    bus: EventBus[_Event] = EventBus()
    queue = bus.subscribe()
    assert queue.empty()


def test_emit_delivers_to_subscriber() -> None:
    bus: EventBus[_Event] = EventBus()
    queue = bus.subscribe()
    bus.emit(_Event(value="a"))
    assert queue.get_nowait() == _Event(value="a")


def test_emit_delivers_to_all_subscribers() -> None:
    bus: EventBus[_Event] = EventBus()
    q1 = bus.subscribe()
    q2 = bus.subscribe()
    bus.emit(_Event(value="a"))
    assert q1.get_nowait() == _Event(value="a")
    assert q2.get_nowait() == _Event(value="a")


def test_emit_before_any_subscriber_is_dropped() -> None:
    """No subscribers yet — emit() must not raise; there's simply no queue to deliver to."""
    bus: EventBus[_Event] = EventBus()
    bus.emit(_Event(value="a"))  # must not raise


def test_unsubscribe_stops_delivery() -> None:
    bus: EventBus[_Event] = EventBus()
    queue = bus.subscribe()
    bus.unsubscribe(queue)
    bus.emit(_Event(value="a"))
    assert queue.empty()


def test_unsubscribe_unknown_queue_is_idempotent() -> None:
    import asyncio

    bus: EventBus[_Event] = EventBus()
    orphan: asyncio.Queue[_Event] = asyncio.Queue()
    bus.unsubscribe(orphan)  # must not raise


def test_full_queue_drops_event_silently() -> None:
    bus: EventBus[_Event] = EventBus(queue_max=1)
    queue = bus.subscribe()
    bus.emit(_Event(value="a"))
    bus.emit(_Event(value="b"))  # queue is full — dropped, not raised
    assert queue.get_nowait() == _Event(value="a")
    assert queue.empty()
