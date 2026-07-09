"""Unit tests for the WebSocket event stream's fan-in mechanism.

Tests ``_forward`` and the merge-multiple-sources-into-one-queue behavior
directly rather than through a real WebSocket connection: FastAPI's test
client runs the ASGI app in a separate thread with its own event loop, and
pushing events into an ``asyncio.Queue`` owned by that other thread's loop
from the test thread is not thread-safe. Exercising ``_forward`` in-process,
in the same event loop as the test, avoids that entirely while covering
exactly the mechanism ``make_ws_router`` relies on — see
docs/adr/035-state-ownership-and-signal-pipeline.md.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass

from partyboxd.api.ws import _forward


@dataclass(frozen=True)
class _Event:
    value: str


class _FakeSource:
    def __init__(self) -> None:
        self._queues: list[asyncio.Queue[_Event]] = []
        self.unsubscribed: list[asyncio.Queue[_Event]] = []

    def subscribe(self) -> asyncio.Queue[_Event]:
        q: asyncio.Queue[_Event] = asyncio.Queue()
        self._queues.append(q)
        return q

    def unsubscribe(self, queue: asyncio.Queue[_Event]) -> None:
        self.unsubscribed.append(queue)
        if queue in self._queues:
            self._queues.remove(queue)

    def emit(self, event: _Event) -> None:
        for q in self._queues:
            q.put_nowait(event)


async def _cancel(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def test_forward_relays_events_into_sink() -> None:
    source = _FakeSource()
    sink: asyncio.Queue[_Event] = asyncio.Queue()
    task = asyncio.create_task(_forward(source, sink))
    await asyncio.sleep(0)  # let _forward reach queue.get()

    source.emit(_Event(value="x"))
    event = await asyncio.wait_for(sink.get(), timeout=1.0)
    assert event == _Event(value="x")

    await _cancel(task)


async def test_forward_unsubscribes_on_cancel() -> None:
    source = _FakeSource()
    sink: asyncio.Queue[_Event] = asyncio.Queue()
    task = asyncio.create_task(_forward(source, sink))
    await asyncio.sleep(0)

    await _cancel(task)
    assert len(source.unsubscribed) == 1


async def test_multiple_sources_merge_into_one_sink() -> None:
    """This is the actual fan-in the WS endpoint depends on: events from N
    independent sources (DeviceManager + companion's Audio/Spotify/Pairing
    services) all land in the same merged queue, in whatever order they
    were emitted."""
    manager = _FakeSource()
    audio = _FakeSource()
    spotify = _FakeSource()
    sink: asyncio.Queue[_Event] = asyncio.Queue()

    tasks = [asyncio.create_task(_forward(src, sink)) for src in (manager, audio, spotify)]
    await asyncio.sleep(0)

    manager.emit(_Event(value="from-manager"))
    audio.emit(_Event(value="from-audio"))
    spotify.emit(_Event(value="from-spotify"))

    received = {(await asyncio.wait_for(sink.get(), timeout=1.0)).value for _ in range(3)}
    assert received == {"from-manager", "from-audio", "from-spotify"}

    for task in tasks:
        await _cancel(task)
