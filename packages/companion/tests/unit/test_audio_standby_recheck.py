"""Unit tests for _recheck_audio_on_standby in companion.__main__.

Nudges AudioService to re-check A2DP as soon as the BLE control link reports
the speaker leaving "on", instead of waiting out AudioService's own 60s idle
check interval (ADR-028, ADR-034).
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from unittest.mock import MagicMock

from companion.__main__ import _recheck_audio_on_standby
from companion.services.audio import AudioService
from partyboxd.device.events import ConnectedEvent, DeviceEvent, SpeakerStateChangedEvent


def _manager_mock(q: asyncio.Queue[DeviceEvent]) -> MagicMock:
    m = MagicMock()
    m.subscribe.return_value = q
    return m


async def test_recheck_now_called_on_transition_to_standby() -> None:
    q: asyncio.Queue[DeviceEvent] = asyncio.Queue()
    q.put_nowait(SpeakerStateChangedEvent(state="standby"))
    audio = MagicMock(spec=AudioService)

    task = asyncio.create_task(_recheck_audio_on_standby(_manager_mock(q), audio))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    audio.recheck_now.assert_called_once()
    audio.retry_now.assert_not_called()

    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def test_recheck_now_called_on_transition_to_off() -> None:
    q: asyncio.Queue[DeviceEvent] = asyncio.Queue()
    q.put_nowait(SpeakerStateChangedEvent(state="off"))
    audio = MagicMock(spec=AudioService)

    task = asyncio.create_task(_recheck_audio_on_standby(_manager_mock(q), audio))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    audio.recheck_now.assert_called_once()

    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def test_transition_to_on_fires_retry_not_recheck() -> None:
    """Waking up is the strong signal: it must break A2DP out of any
    failure back-off/cool-down (retry_now), not merely poke the idle wait
    (recheck_now) — a speaker powered on from the Portal right after a
    failure run otherwise sat silent for up to the full 300s cool-down."""
    q: asyncio.Queue[DeviceEvent] = asyncio.Queue()
    q.put_nowait(SpeakerStateChangedEvent(state="on"))
    audio = MagicMock(spec=AudioService)

    task = asyncio.create_task(_recheck_audio_on_standby(_manager_mock(q), audio))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    audio.recheck_now.assert_not_called()
    audio.retry_now.assert_called_once_with("speaker woke up")

    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def test_ignores_unrelated_device_events() -> None:
    q: asyncio.Queue[DeviceEvent] = asyncio.Queue()
    q.put_nowait(ConnectedEvent(address="AA:BB:CC:DD:EE:FF", firmware="26.2.10", battery=90))
    audio = MagicMock(spec=AudioService)

    task = asyncio.create_task(_recheck_audio_on_standby(_manager_mock(q), audio))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    audio.recheck_now.assert_not_called()

    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def test_unsubscribes_on_cancel() -> None:
    q: asyncio.Queue[DeviceEvent] = asyncio.Queue()
    manager = _manager_mock(q)
    audio = MagicMock(spec=AudioService)

    task = asyncio.create_task(_recheck_audio_on_standby(manager, audio))
    await asyncio.sleep(0)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    manager.unsubscribe.assert_called_once_with(q)
