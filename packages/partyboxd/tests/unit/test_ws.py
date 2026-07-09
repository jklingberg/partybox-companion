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
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from partyboxd.api.ws import _forward, make_ws_router
from partyboxd.config import ApiSettings, Settings


@dataclass(frozen=True)
class _Event:
    value: str
    type: str = "fake_event"


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


async def _wait_until_subscribed(*sources: _FakeSource) -> None:
    """Poll until every source has at least one subscriber.

    ws_events itself no longer has a race here: it calls source.subscribe()
    synchronously for every source *before* creating any forwarder task (see
    _forward's docstring and test_subscribe_before_forwarder_task_closes_
    connection_start_race), so subscription is already done by the time the
    endpoint task first yields. This helper is defensive belt-and-braces for
    the test itself -- it doesn't assume exactly how many ticks the endpoint
    task needs to reach that point -- not a workaround for a race in the
    production code.
    """
    for _ in range(200):
        if all(len(s._queues) > 0 for s in sources):
            return
        await asyncio.sleep(0.005)
    raise AssertionError("sources were never subscribed")


async def test_forward_relays_events_into_sink() -> None:
    source = _FakeSource()
    sink: asyncio.Queue[_Event] = asyncio.Queue()
    task = asyncio.create_task(_forward(source, source.subscribe(), sink))
    await asyncio.sleep(0)  # let _forward reach queue.get()

    source.emit(_Event(value="x"))
    event = await asyncio.wait_for(sink.get(), timeout=1.0)
    assert event == _Event(value="x")

    await _cancel(task)


async def test_forward_unsubscribes_on_cancel() -> None:
    source = _FakeSource()
    sink: asyncio.Queue[_Event] = asyncio.Queue()
    task = asyncio.create_task(_forward(source, source.subscribe(), sink))
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

    tasks = [
        asyncio.create_task(_forward(src, src.subscribe(), sink))
        for src in (manager, audio, spotify)
    ]
    await asyncio.sleep(0)

    manager.emit(_Event(value="from-manager"))
    audio.emit(_Event(value="from-audio"))
    spotify.emit(_Event(value="from-spotify"))

    received = {(await asyncio.wait_for(sink.get(), timeout=1.0)).value for _ in range(3)}
    assert received == {"from-manager", "from-audio", "from-spotify"}

    for task in tasks:
        await _cancel(task)


async def test_events_from_one_source_preserve_emission_order() -> None:
    """Ordering is only guaranteed per-source (docs/adr/036-push-not-poll-
    ws-fanin.md's ordering section): a single source's own emit() sequence
    must survive intact through _forward into the merged sink, in order."""
    source = _FakeSource()
    sink: asyncio.Queue[_Event] = asyncio.Queue()
    task = asyncio.create_task(_forward(source, source.subscribe(), sink))
    await asyncio.sleep(0)

    source.emit(_Event(value="first"))
    source.emit(_Event(value="second"))
    source.emit(_Event(value="third"))

    received = [(await asyncio.wait_for(sink.get(), timeout=1.0)).value for _ in range(3)]
    assert received == ["first", "second", "third"]

    await _cancel(task)


async def test_subscribe_before_forwarder_task_closes_connection_start_race() -> None:
    """Regression test for the connection-start race documented in
    docs/adr/036-push-not-poll-ws-fanin.md: subscribing synchronously
    *before* creating the forwarder task means an event emitted the instant
    after task creation -- with no `await asyncio.sleep(0)` to let the task
    actually start running first -- is still captured, because the queue it
    lands in already exists and is already registered on the source."""
    source = _FakeSource()
    sink: asyncio.Queue[_Event] = asyncio.Queue()

    queue = source.subscribe()  # synchronous, as ws_events now does it
    task = asyncio.create_task(_forward(source, queue, sink))
    source.emit(_Event(value="emitted-before-task-ran"))  # no yield in between

    event = await asyncio.wait_for(sink.get(), timeout=1.0)
    assert event == _Event(value="emitted-before-task-ran")

    await _cancel(task)


# ---------------------------------------------------------------------------
# End-to-end: the real ws_events handler, dataclass -> asdict -> JSON.
#
# Deliberately not FastAPI TestClient (see module docstring for why). Instead,
# the actual handler function is pulled off the router FastAPI registered it
# on (APIWebSocketRoute.endpoint is the original, unwrapped coroutine) and
# called directly against a minimal fake WebSocket -- same event loop as the
# test, no threads, no real ASGI transport, no hardware. This exercises the
# real make_ws_router/_forward/dataclasses.asdict/send_json call chain the
# fan-in unit tests above cover only piecemeal.
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    """Just enough of Starlette's WebSocket surface for ws_events to run."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.accepted = False
        self.closed_code: int | None = None
        self.client = None  # ws.py logs websocket.client on connect/disconnect

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, data: dict[str, Any]) -> None:
        json.dumps(data)  # must actually be JSON-serializable, not just a dict
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        self.closed_code = code


def _ws_endpoint(
    manager: object, settings: Settings | None = None, extra_sources: tuple[object, ...] = ()
) -> Callable[..., Awaitable[None]]:
    router = make_ws_router(manager, settings or Settings(), extra_sources=extra_sources)  # type: ignore[arg-type]
    (route,) = router.routes
    return route.endpoint  # type: ignore[attr-defined]


class _Color(StrEnum):
    """Stands in for a real event field that's a StrEnum (e.g. PairingState
    in companion) -- confirms json.dumps handles it without a custom encoder,
    the same way it does for the real pairing_progress event's `state`."""

    RED = "red"


@dataclass(frozen=True)
class _EnumEvent:
    color: _Color
    type: str = "enum_event"


async def test_ws_events_delivers_real_json_serializable_payloads() -> None:
    manager = _FakeSource()
    audio = _FakeSource()
    endpoint = _ws_endpoint(manager, extra_sources=(audio,))
    ws = _FakeWebSocket()

    task = asyncio.create_task(endpoint(ws, api_key=None))
    await _wait_until_subscribed(manager, audio)

    manager.emit(_Event(value="from-manager"))
    audio.emit(_EnumEvent(color=_Color.RED))

    for _ in range(200):
        if len(ws.sent) >= 2:
            break
        await asyncio.sleep(0.005)

    await _cancel(task)

    assert ws.accepted
    by_type = {item.get("type"): item for item in ws.sent}
    assert by_type["fake_event"] == {"value": "from-manager", "type": "fake_event"}
    # StrEnum survives asdict + json.dumps as its plain string value, not an
    # enum repr -- this is the concrete case docs/adr/036 flags for
    # PairingProgressEvent.state (a real PairingState StrEnum) in companion.
    assert by_type["enum_event"] == {"color": "red", "type": "enum_event"}


async def test_ws_events_closes_with_4001_on_bad_api_key() -> None:
    manager = _FakeSource()
    settings = Settings(api=ApiSettings(api_key="secret"))
    endpoint = _ws_endpoint(manager, settings=settings)
    ws = _FakeWebSocket()

    await endpoint(ws, api_key="wrong")

    assert not ws.accepted
    assert ws.closed_code == 4001


async def test_ws_events_accepts_correct_api_key() -> None:
    manager = _FakeSource()
    settings = Settings(api=ApiSettings(api_key="secret"))
    endpoint = _ws_endpoint(manager, settings=settings)
    ws = _FakeWebSocket()

    task = asyncio.create_task(endpoint(ws, api_key="secret"))
    await asyncio.sleep(0)

    assert ws.accepted
    assert ws.closed_code is None

    await _cancel(task)
