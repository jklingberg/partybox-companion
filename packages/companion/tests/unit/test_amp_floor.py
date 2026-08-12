"""Unit tests for _maintain_amp_floor in companion.__main__.

The connect-time pin (``pin_volume_fn``) is not enough on its own: the speaker
reverts its amplifier to its own remembered level *during* a live session, not
just on a new transport (observed 2026-08-12 — the floor set 64, and ten minutes
later on the same transport it read 40 again). This loop re-asserts the floor so
INC-2's symptom cannot come back mid-listen.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock, MagicMock, patch

from companion.__main__ import _maintain_amp_floor
from companion.services.audio import AudioService

_ADDRESS = "50:1B:6A:14:FD:1D"
_TICK = 0.01


def _audio(*, ready: bool = True, address: str | None = _ADDRESS) -> MagicMock:
    audio = MagicMock(spec=AudioService)
    audio.audio_ready = ready
    audio.status = MagicMock(address=address)
    return audio


async def _run_briefly(audio: MagicMock, baseline: int, raiser: AsyncMock) -> None:
    """Run the loop long enough for a few ticks, then cancel it."""
    with patch("companion.__main__.avrcp_volume.raise_amp_to_baseline", raiser):
        task = asyncio.create_task(_maintain_amp_floor(audio, baseline, interval=_TICK))
        await asyncio.sleep(_TICK * 6)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_reasserts_the_floor_while_audio_is_ready() -> None:
    raiser = AsyncMock()
    await _run_briefly(_audio(), 52, raiser)
    assert raiser.await_count >= 1
    raiser.assert_awaited_with(_ADDRESS, 52)


async def test_reasserts_repeatedly_not_just_once() -> None:
    """A single write is what failed on hardware — the point is that it recurs."""
    raiser = AsyncMock()
    await _run_briefly(_audio(), 52, raiser)
    assert raiser.await_count >= 2


async def test_passes_the_configured_baseline_through() -> None:
    raiser = AsyncMock()
    await _run_briefly(_audio(), 96, raiser)
    raiser.assert_awaited_with(_ADDRESS, 96)


async def test_skips_while_audio_not_ready() -> None:
    """No transport means nothing to pin — don't spawn a helper per tick."""
    raiser = AsyncMock()
    await _run_briefly(_audio(ready=False), 52, raiser)
    raiser.assert_not_awaited()


async def test_skips_when_address_unknown() -> None:
    raiser = AsyncMock()
    await _run_briefly(_audio(address=None), 52, raiser)
    raiser.assert_not_awaited()


async def test_sleeps_before_first_check() -> None:
    """The connect-time pin already ran; don't duplicate it immediately."""
    raiser = AsyncMock()
    with patch("companion.__main__.avrcp_volume.raise_amp_to_baseline", raiser):
        task = asyncio.create_task(_maintain_amp_floor(_audio(), 52, interval=10.0))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        raiser.assert_not_awaited()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_rechecks_readiness_every_tick() -> None:
    """Readiness is re-read per tick, so a mid-session drop stops the writes."""
    audio = _audio()
    raiser = AsyncMock()
    with patch("companion.__main__.avrcp_volume.raise_amp_to_baseline", raiser):
        task = asyncio.create_task(_maintain_amp_floor(audio, 52, interval=_TICK))
        await asyncio.sleep(_TICK * 3)
        first = raiser.await_count
        assert first >= 1
        audio.audio_ready = False
        await asyncio.sleep(_TICK * 4)
        assert raiser.await_count == first
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_unexpected_failure_propagates_to_supervisor() -> None:
    """Expected failures are absorbed by raise_amp_to_baseline; bugs must not be."""
    raiser = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("companion.__main__.avrcp_volume.raise_amp_to_baseline", raiser):
        task = asyncio.create_task(_maintain_amp_floor(_audio(), 52, interval=_TICK))
        with suppress(asyncio.CancelledError):
            try:
                await asyncio.wait_for(task, timeout=1.0)
            except RuntimeError as exc:
                assert str(exc) == "boom"
            else:  # pragma: no cover - the loop must not swallow it
                raise AssertionError("RuntimeError did not propagate")
