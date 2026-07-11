"""Unit tests for _idle_battery_shutdown in companion.__main__ (ADR-038).

Powers off the Pi after sustained idle-on-battery time, always on (no
disable switch, no configurable threshold — see _STANDBY_GRACE_SECONDS/
_OFF_STATE_GRACE_SECONDS docstrings in companion.__main__ for why the
values are fixed rather than Portal-configurable).
_IDLE_SHUTDOWN_CHECK_INTERVAL is patched to 0 so the watcher's loop ticks as
fast as asyncio scheduling allows; time.monotonic is patched to a manually-
advanced fake clock so elapsed-time thresholds are deterministic regardless
of real wall-clock speed.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock, MagicMock

import pytest
from companion import __main__ as main_module
from companion.__main__ import _idle_battery_shutdown
from partybox.protocol.messages import BatteryStatusResponse, ChargingStatus
from partyboxd.device.manager import StatusSnapshot


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _snapshot(state: str, on_mains: bool | None = None) -> StatusSnapshot:
    """Build a StatusSnapshot whose derived speaker_state is *state*."""
    if state == "off":
        return StatusSnapshot(
            connected=False,
            address=None,
            firmware=None,
            battery=None,
            battery_status=None,
            has_battery=False,
            speaker_awake=True,
        )
    charging_status = (
        None
        if on_mains is None
        else (ChargingStatus.CHARGING if on_mains else ChargingStatus.DISCHARGING)
    )
    return StatusSnapshot(
        connected=True,
        address="AA:BB:CC:DD:EE:FF",
        firmware="26.2.10",
        battery=50,
        battery_status=BatteryStatusResponse(charging_status=charging_status),
        has_battery=True,
        speaker_awake=(state == "on"),
    )


async def _settle() -> None:
    # Two yields: one for the watcher's `await asyncio.sleep(interval)` to
    # resume, one for its synchronous body to run to completion.
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def _fast_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "_IDLE_SHUTDOWN_CHECK_INTERVAL", 0.0)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    fake = _FakeClock()
    monkeypatch.setattr(main_module.time, "monotonic", fake)
    return fake


async def _run_watcher(manager: MagicMock, power_off: AsyncMock) -> asyncio.Task[None]:
    task = asyncio.create_task(_idle_battery_shutdown(manager, power_off))
    await _settle()
    return task


async def _cancel(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def test_triggers_after_threshold_on_battery_and_standby(clock: _FakeClock) -> None:
    manager = MagicMock()
    manager.snapshot = _snapshot("standby", on_mains=False)
    power_off = AsyncMock()

    task = await _run_watcher(manager, power_off)

    clock.now = main_module._STANDBY_GRACE_SECONDS - 5.0
    await _settle()
    power_off.assert_not_called()

    clock.now = main_module._STANDBY_GRACE_SECONDS + 5.0
    await _settle()
    power_off.assert_called_once()

    await _cancel(task)


async def test_never_triggers_on_mains(clock: _FakeClock) -> None:
    manager = MagicMock()
    manager.snapshot = _snapshot("standby", on_mains=True)
    power_off = AsyncMock()

    task = await _run_watcher(manager, power_off)
    for t in (0.0, 30.0, main_module._STANDBY_GRACE_SECONDS + 60.0):
        clock.now = t
        await _settle()

    power_off.assert_not_called()
    await _cancel(task)


async def test_never_triggers_while_speaker_on(clock: _FakeClock) -> None:
    manager = MagicMock()
    manager.snapshot = _snapshot("on", on_mains=False)
    power_off = AsyncMock()

    task = await _run_watcher(manager, power_off)
    for t in (0.0, main_module._STANDBY_GRACE_SECONDS + 60.0):
        clock.now = t
        await _settle()

    power_off.assert_not_called()
    await _cancel(task)


async def test_never_triggers_on_cold_off_with_no_prior_confirmed_battery(
    clock: _FakeClock,
) -> None:
    """No prior confirmed reading (e.g. companion started while the speaker
    was already fully off) means last_known_on_battery stays None — must
    not treat that as "on battery"."""
    manager = MagicMock()
    manager.snapshot = _snapshot("off")
    power_off = AsyncMock()

    task = await _run_watcher(manager, power_off)
    for t in (0.0, main_module._OFF_STATE_GRACE_SECONDS + 60.0):
        clock.now = t
        await _settle()

    power_off.assert_not_called()
    await _cancel(task)


async def test_off_state_fires_almost_immediately_after_long_standby_idle(
    clock: _FakeClock,
) -> None:
    """Hardware validation (ADR-038 follow-up): the PartyBox can auto-power-off
    past standby into a state where BLE goes fully dark, leaving the Pi
    scanning forever, fully powered, unable to reach it — the same drain
    this feature exists to prevent. The idle clock is a single continuous
    counter that survives the standby -> off transition; only the threshold
    compared against it changes. A speaker idle a long time in standby
    (well under _STANDBY_GRACE_SECONDS) that then drops to "off" must fire
    against the much shorter _OFF_STATE_GRACE_SECONDS, not wait out the rest
    of the standby threshold."""
    manager = MagicMock()
    manager.snapshot = _snapshot("standby", on_mains=False)
    power_off = AsyncMock()

    task = await _run_watcher(manager, power_off)

    long_idle = (
        main_module._STANDBY_GRACE_SECONDS - 100.0
    )  # long, still under standby's own threshold
    clock.now = long_idle
    await _settle()
    power_off.assert_not_called()

    # BLE drops entirely — total idle time already dwarfs the fixed
    # off-state grace period, so this must fire on essentially the next
    # tick, not wait out the remaining ~100s of the standby threshold.
    manager.snapshot = _snapshot("off")
    clock.now = long_idle + main_module._OFF_STATE_GRACE_SECONDS + 1.0
    await _settle()
    power_off.assert_called_once()

    await _cancel(task)


async def test_never_triggers_on_off_state_before_its_grace_period_elapses(
    clock: _FakeClock,
) -> None:
    """A speaker that goes straight from "on" to "off" (BLE drops abruptly,
    skipping a standby snapshot entirely) must not fire before
    _OFF_STATE_GRACE_SECONDS — this debounces an ordinary transient BLE
    reconnect blip, which DeviceManager normally recovers from on its own
    within seconds."""
    manager = MagicMock()
    manager.snapshot = _snapshot("on", on_mains=False)
    power_off = AsyncMock()

    task = await _run_watcher(manager, power_off)

    # First tick where "off" is observed anchors idle_since at clock.now — 0
    # here, so later checks are simple offsets from that anchor.
    manager.snapshot = _snapshot("off")
    await _settle()
    power_off.assert_not_called()

    clock.now = main_module._OFF_STATE_GRACE_SECONDS - 5.0
    await _settle()
    power_off.assert_not_called()

    clock.now = main_module._OFF_STATE_GRACE_SECONDS + 5.0
    await _settle()
    power_off.assert_called_once()

    await _cancel(task)


async def test_never_triggers_on_off_when_last_known_was_mains(clock: _FakeClock) -> None:
    manager = MagicMock()
    manager.snapshot = _snapshot("standby", on_mains=True)
    power_off = AsyncMock()

    task = await _run_watcher(manager, power_off)
    manager.snapshot = _snapshot("off")
    for t in (0.0, main_module._OFF_STATE_GRACE_SECONDS + 60.0):
        clock.now = t
        await _settle()

    power_off.assert_not_called()
    await _cancel(task)


async def test_resets_idle_clock_when_activity_resumes(clock: _FakeClock) -> None:
    manager = MagicMock()
    manager.snapshot = _snapshot("standby", on_mains=False)
    power_off = AsyncMock()

    task = await _run_watcher(manager, power_off)

    clock.now = main_module._STANDBY_GRACE_SECONDS - 50.0
    await _settle()
    power_off.assert_not_called()

    # Activity resumes just before the threshold would have fired.
    manager.snapshot = _snapshot("on", on_mains=False)
    clock.now = main_module._STANDBY_GRACE_SECONDS - 45.0
    await _settle()

    # Idle again — the clock must have restarted from here, not from t=0.
    manager.snapshot = _snapshot("standby", on_mains=False)
    reset_point = main_module._STANDBY_GRACE_SECONDS - 40.0
    clock.now = reset_point
    await _settle()

    clock.now = reset_point + main_module._STANDBY_GRACE_SECONDS - 5.0
    await _settle()
    power_off.assert_not_called()

    clock.now = reset_point + main_module._STANDBY_GRACE_SECONDS + 5.0
    await _settle()
    power_off.assert_called_once()

    await _cancel(task)


async def test_does_not_retrigger_after_first_shutdown(clock: _FakeClock) -> None:
    manager = MagicMock()
    manager.snapshot = _snapshot("standby", on_mains=False)
    power_off = AsyncMock()

    task = await _run_watcher(manager, power_off)

    clock.now = main_module._STANDBY_GRACE_SECONDS + 5.0
    await _settle()
    power_off.assert_called_once()

    clock.now = main_module._STANDBY_GRACE_SECONDS + 10000.0
    await _settle()
    power_off.assert_called_once()

    await _cancel(task)
