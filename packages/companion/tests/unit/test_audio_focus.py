"""Unit tests for AudioFocusService — competing-source detection.

Scan results are injected via ``scan_fn``; the payload fixtures are the real
PartyBox 520 btmon captures from docs/reverse-engineering/protocol.md
§ "FDDF Advertisement" (2026-07-16), never fabricated.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

from companion.services.audio_focus import AudioFocus, AudioFocusChanged, AudioFocusService

FDDF_PHONE_CONNECTED = bytes.fromhex("202101d453e2c70c06586b501b6a14fd1d00090000000000")
FDDF_COMPANION_ONLY = bytes.fromhex("202101d453e2c70c05586b501b6a14fd1d00010000000000")

_ADDRESS = "50:1B:6A:14:FD:1D"


class _ScriptedScan:
    """scan_fn stub yielding queued results; None forever once exhausted."""

    def __init__(self, results: list[bytes | None]) -> None:
        self._results = list(results)
        self.calls: list[str] = []

    async def __call__(self, address: str) -> bytes | None:
        self.calls.append(address)
        return self._results.pop(0) if self._results else None


async def _drive(service: AudioFocusService, until: asyncio.Event, timeout: float = 2.0) -> None:
    """Run service.run() until *until* is set, then cancel it."""
    task = asyncio.create_task(service.run())
    try:
        await asyncio.wait_for(until.wait(), timeout=timeout)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def _wait_for_focus(service: AudioFocusService, focus: AudioFocus) -> None:
    queue = service.subscribe()
    try:
        while True:
            event = await queue.get()
            if event.focus == focus:
                return
    finally:
        service.unsubscribe(queue)


async def test_companion_only_classifies_exclusive() -> None:
    scan = _ScriptedScan([FDDF_COMPANION_ONLY])
    service = AudioFocusService(lambda: _ADDRESS, lambda: False, scan_fn=scan, interval=0.01)
    done = asyncio.Event()

    async def watch() -> None:
        await _wait_for_focus(service, AudioFocus.EXCLUSIVE)
        done.set()

    watcher = asyncio.create_task(watch())
    await _drive(service, done)
    await watcher
    assert service.focus is AudioFocus.EXCLUSIVE
    assert scan.calls[0] == _ADDRESS


async def test_phone_connected_classifies_contested() -> None:
    scan = _ScriptedScan([FDDF_PHONE_CONNECTED])
    service = AudioFocusService(lambda: _ADDRESS, lambda: False, scan_fn=scan, interval=0.01)
    done = asyncio.Event()

    async def watch() -> None:
        await _wait_for_focus(service, AudioFocus.CONTESTED)
        done.set()

    watcher = asyncio.create_task(watch())
    await _drive(service, done)
    await watcher
    assert service.focus is AudioFocus.CONTESTED


async def test_transition_emits_event_and_misses_eventually_reset_to_unknown() -> None:
    # One exclusive reading, one contested, then only misses: the state must
    # ride through the first two misses unchanged and drop to UNKNOWN on the
    # third consecutive miss.
    scan = _ScriptedScan([FDDF_COMPANION_ONLY, FDDF_PHONE_CONNECTED])
    service = AudioFocusService(lambda: _ADDRESS, lambda: False, scan_fn=scan, interval=0.01)
    queue = service.subscribe()
    done = asyncio.Event()

    seen: list[str] = []

    async def watch() -> None:
        while True:
            event: AudioFocusChanged = await queue.get()
            seen.append(event.focus)
            if len(seen) >= 4:  # initial unknown + exclusive + contested + unknown
                done.set()
                return

    watcher = asyncio.create_task(watch())
    await _drive(service, done)
    await watcher
    service.unsubscribe(queue)
    assert seen == ["unknown", "exclusive", "contested", "unknown"]
    # Contested survived exactly two misses before the third reset it.
    assert len(scan.calls) >= 5


async def test_no_address_means_unknown_and_no_scan() -> None:
    scan = _ScriptedScan([FDDF_COMPANION_ONLY])
    service = AudioFocusService(lambda: None, lambda: False, scan_fn=scan, interval=0.01)

    task = asyncio.create_task(service.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    assert service.focus is AudioFocus.UNKNOWN
    assert scan.calls == []


async def test_scans_are_skipped_while_pairing_is_active() -> None:
    scan = _ScriptedScan([FDDF_COMPANION_ONLY])
    pairing_active = True
    service = AudioFocusService(
        lambda: _ADDRESS, lambda: pairing_active, scan_fn=scan, interval=0.01
    )

    task = asyncio.create_task(service.run())
    await asyncio.sleep(0.05)
    assert scan.calls == []
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def test_streaming_relaxes_cadence_but_scans_continue() -> None:
    # While streaming_fn reports True the loop must pace itself at
    # streaming_interval (interval would stall this test) — and must keep
    # scanning at all: "playing but rendering silence because another source
    # holds the speaker" is the flagship state this service exists to expose.
    scan = _ScriptedScan([FDDF_PHONE_CONNECTED, FDDF_PHONE_CONNECTED, FDDF_PHONE_CONNECTED])

    async def streaming() -> bool:
        return True

    service = AudioFocusService(
        lambda: _ADDRESS,
        lambda: False,
        scan_fn=scan,
        interval=30.0,
        streaming_fn=streaming,
        streaming_interval=0.01,
    )
    done = asyncio.Event()

    async def watch() -> None:
        await _wait_for_focus(service, AudioFocus.CONTESTED)
        while len(scan.calls) < 2:  # a second scan proves the sleep was 0.01
            await asyncio.sleep(0.005)
        done.set()

    watcher = asyncio.create_task(watch())
    await _drive(service, done)
    await watcher
    assert service.focus is AudioFocus.CONTESTED
    assert len(scan.calls) >= 2


async def test_not_streaming_uses_normal_cadence() -> None:
    scan = _ScriptedScan([FDDF_COMPANION_ONLY, FDDF_COMPANION_ONLY])

    async def streaming() -> bool:
        return False

    service = AudioFocusService(
        lambda: _ADDRESS,
        lambda: False,
        scan_fn=scan,
        interval=0.01,
        streaming_fn=streaming,
        streaming_interval=30.0,  # would stall the test if wrongly chosen
    )
    done = asyncio.Event()

    async def watch() -> None:
        while len(scan.calls) < 2:
            await asyncio.sleep(0.005)
        done.set()

    watcher = asyncio.create_task(watch())
    await _drive(service, done)
    await watcher
    assert len(scan.calls) >= 2


async def test_subscribe_delivers_current_state_first() -> None:
    service = AudioFocusService(lambda: None, lambda: False, scan_fn=_ScriptedScan([]))
    queue = service.subscribe()
    first = queue.get_nowait()
    assert first.focus == "unknown"
    assert first.type == "audio_focus_changed"
    service.unsubscribe(queue)
