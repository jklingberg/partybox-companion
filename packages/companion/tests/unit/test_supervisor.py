"""Unit tests for the task Supervisor.

Tests cover:
- normal operation (task runs until cancelled)
- unexpected exception → restart
- unexpected clean return → restart
- cancellation → clean shutdown of all tasks
- restart count and backoff delay progression
- backoff reset after stable runtime
- exit policy calls os._exit
- multiple independent tasks supervised concurrently
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from unittest.mock import patch

import pytest
from companion.supervisor import RestartPolicy, Supervisor, _Entry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _run_supervisor_briefly(supervisor: Supervisor, *, ticks: int = 4) -> None:
    """Start the supervisor, yield a few event-loop ticks, then cancel it."""
    task = asyncio.create_task(supervisor.run())
    for _ in range(ticks):
        await asyncio.sleep(0)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# RestartPolicy defaults
# ---------------------------------------------------------------------------


def test_restart_policy_defaults() -> None:
    p = RestartPolicy()
    assert p.mode == "restart"
    assert p.initial_delay == 5.0
    assert p.max_delay == 300.0
    assert p.backoff_factor == 2.0
    assert p.reset_after == 60.0


# ---------------------------------------------------------------------------
# _Entry.consume_delay — pure backoff arithmetic
# ---------------------------------------------------------------------------


def test_consume_delay_first_call() -> None:
    entry = _Entry("t", lambda: asyncio.sleep(0), RestartPolicy(initial_delay=5.0))
    delay = entry.consume_delay(runtime=0.0)
    assert delay == 5.0
    assert entry.restart_count == 1


def test_consume_delay_doubles() -> None:
    entry = _Entry(
        "t",
        lambda: asyncio.sleep(0),
        RestartPolicy(initial_delay=1.0, max_delay=100.0, backoff_factor=2.0, reset_after=999.0),
    )
    assert entry.consume_delay(runtime=0.0) == 1.0
    assert entry.consume_delay(runtime=0.0) == 2.0
    assert entry.consume_delay(runtime=0.0) == 4.0
    assert entry.consume_delay(runtime=0.0) == 8.0


def test_consume_delay_capped_at_max() -> None:
    entry = _Entry(
        "t",
        lambda: asyncio.sleep(0),
        RestartPolicy(initial_delay=64.0, max_delay=100.0, backoff_factor=2.0, reset_after=999.0),
    )
    entry.consume_delay(runtime=0.0)  # 64
    delay = entry.consume_delay(runtime=0.0)  # would be 128, capped at 100
    assert delay == 100.0


def test_consume_delay_resets_after_stable_runtime() -> None:
    entry = _Entry(
        "t",
        lambda: asyncio.sleep(0),
        RestartPolicy(initial_delay=1.0, max_delay=100.0, backoff_factor=2.0, reset_after=60.0),
    )
    entry.consume_delay(runtime=0.0)  # delay → 2
    entry.consume_delay(runtime=0.0)  # delay → 4
    # Stable run: runtime >= reset_after resets to initial_delay
    delay = entry.consume_delay(runtime=60.0)
    assert delay == 1.0
    assert entry.restart_count == 1  # reset_count zeroed then incremented to 1


# ---------------------------------------------------------------------------
# Normal operation: task runs until supervisor cancelled
# ---------------------------------------------------------------------------


async def test_task_runs_until_cancelled() -> None:
    """A well-behaved perpetual task should start once and not be restarted."""
    starts: list[int] = []

    async def forever() -> None:
        starts.append(1)
        await asyncio.sleep(1000)

    supervisor = Supervisor()
    supervisor.register("forever", forever, policy=RestartPolicy(initial_delay=0.0))
    await _run_supervisor_briefly(supervisor)

    assert len(starts) == 1


async def test_supervisor_empty_returns_immediately() -> None:
    """Supervisor with no registered tasks should return without hanging."""
    supervisor = Supervisor()
    # Should not raise, should not hang
    await asyncio.wait_for(supervisor.run(), timeout=1.0)


# ---------------------------------------------------------------------------
# Restart on unexpected exception
# ---------------------------------------------------------------------------


async def test_restarts_after_exception() -> None:
    """Task that raises an exception should be restarted."""
    starts: list[int] = []
    stable = asyncio.Event()

    async def crashes_once() -> None:
        starts.append(1)
        if len(starts) == 1:
            raise RuntimeError("simulated crash")
        stable.set()
        # Raw Future instead of asyncio.sleep so the backoff-sleep mock in other
        # tests doesn't bleed through, and we don't accidentally sleep 1000 s.
        await asyncio.get_running_loop().create_future()

    supervisor = Supervisor()
    supervisor.register("crasher", crashes_once, policy=RestartPolicy(initial_delay=0.0))

    task = asyncio.create_task(supervisor.run())
    await asyncio.wait_for(stable.wait(), timeout=2.0)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert len(starts) == 2


async def test_restarts_after_clean_return() -> None:
    """Task that returns None (unexpected) should be treated as a failure."""
    starts: list[int] = []
    stable = asyncio.Event()

    async def exits_once() -> None:
        starts.append(1)
        if len(starts) < 3:
            return  # unexpected clean exit
        stable.set()
        await asyncio.get_running_loop().create_future()

    supervisor = Supervisor()
    supervisor.register("exiter", exits_once, policy=RestartPolicy(initial_delay=0.0))

    task = asyncio.create_task(supervisor.run())
    await asyncio.wait_for(stable.wait(), timeout=2.0)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert len(starts) == 3


# ---------------------------------------------------------------------------
# Cancellation propagates cleanly
# ---------------------------------------------------------------------------


async def test_cancellation_stops_supervised_task() -> None:
    """Cancelling the supervisor should cancel the inner task."""
    cancelled: list[bool] = []

    async def tracks_cancel() -> None:
        try:
            await asyncio.sleep(1000)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    supervisor = Supervisor()
    supervisor.register("tracked", tracks_cancel)
    task = asyncio.create_task(supervisor.run())
    # Let the inner task start.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert cancelled == [True]


async def test_supervisor_cancellation_raises_cancelled_error() -> None:
    """run() must propagate CancelledError when cancelled."""

    async def forever() -> None:
        await asyncio.sleep(1000)

    supervisor = Supervisor()
    supervisor.register("forever", forever)
    task = asyncio.create_task(supervisor.run())
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# Backoff: delays grow across consecutive failures
# ---------------------------------------------------------------------------


async def test_backoff_delays_grow() -> None:
    """Each consecutive crash should use a longer restart delay."""
    recorded: list[float] = []
    stable = asyncio.Event()
    crashes = 0

    async def crashes_three_times() -> None:
        nonlocal crashes
        crashes += 1
        if crashes < 4:
            raise RuntimeError("crash")
        stable.set()
        # Use a raw Future instead of asyncio.sleep so the patch on
        # companion.supervisor.asyncio.sleep does not capture this wait.
        await asyncio.get_running_loop().create_future()

    async def recording_sleep(delay: float) -> None:
        recorded.append(delay)

    supervisor = Supervisor()
    policy = RestartPolicy(
        initial_delay=1.0, max_delay=100.0, backoff_factor=2.0, reset_after=9999.0
    )
    supervisor.register("crasher", crashes_three_times, policy=policy)

    with patch("companion.supervisor.asyncio.sleep", side_effect=recording_sleep):
        task = asyncio.create_task(supervisor.run())
        await asyncio.wait_for(stable.wait(), timeout=2.0)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    assert recorded == [1.0, 2.0, 4.0]


async def test_backoff_resets_after_stable_run() -> None:
    """Delay resets to initial_delay when the task ran stably long enough.

    Scenario: crash → crash → stable-run → crash.
    Expected delays: [d, 2d, d]  where d = initial_delay.

    We cannot mock time.monotonic globally because asyncio's event loop calls
    loop.time() (= time.monotonic()) constantly for I/O scheduling, making
    scripted values shift unpredictably.  Instead we use real time with a very
    small reset_after (1 ms) and let call 3 actually sleep for 2 ms.  We save a
    reference to the real asyncio.sleep BEFORE patching so the factory can use
    it directly without going through the mock.
    """
    import asyncio as _asyncio_mod

    _real_sleep = _asyncio_mod.sleep  # captured before the patch context

    recorded: list[float] = []
    call_count = 0
    done = asyncio.Event()

    async def factory() -> None:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise RuntimeError("crash")
        if call_count == 3:
            # Real 2 ms sleep so time.monotonic() registers runtime >= reset_after=1 ms.
            await _real_sleep(0.002)
            return
        done.set()
        await asyncio.get_running_loop().create_future()

    async def recording_sleep(delay: float) -> None:
        recorded.append(delay)

    supervisor = Supervisor()
    policy = RestartPolicy(
        initial_delay=0.001, max_delay=10.0, backoff_factor=2.0, reset_after=0.001
    )
    supervisor.register("unstable", factory, policy=policy)

    with patch("companion.supervisor.asyncio.sleep", side_effect=recording_sleep):
        task = asyncio.create_task(supervisor.run())
        await asyncio.wait_for(done.wait(), timeout=5.0)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    # crash 1 → delay 0.001 (initial); crash 2 → delay 0.002 (doubled)
    # stable run (real 2 ms ≥ reset_after 1 ms) → reset → delay 0.001 again
    assert recorded[:3] == [0.001, 0.002, 0.001]


# ---------------------------------------------------------------------------
# Multiple tasks supervised independently
# ---------------------------------------------------------------------------


async def test_multiple_tasks_run_independently() -> None:
    """Failure in one task must not affect other tasks."""
    a_starts: list[int] = []
    b_starts: list[int] = []
    b_stable = asyncio.Event()

    async def task_a_crashes() -> None:
        a_starts.append(1)
        raise RuntimeError("task A crash")

    async def task_b_stable() -> None:
        b_starts.append(1)
        b_stable.set()
        await asyncio.get_running_loop().create_future()

    supervisor = Supervisor()
    supervisor.register("a", task_a_crashes, policy=RestartPolicy(initial_delay=0.0))
    supervisor.register("b", task_b_stable)

    task = asyncio.create_task(supervisor.run())
    await asyncio.wait_for(b_stable.wait(), timeout=2.0)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    # Task B ran stably throughout; task A crashed and was being restarted.
    assert b_starts == [1]
    assert len(a_starts) >= 1  # crashed at least once


async def test_multiple_tasks_all_cancelled_on_shutdown() -> None:
    """All tasks must receive CancelledError when the supervisor is cancelled."""
    running: set[str] = set()
    cancelled: set[str] = set()

    async def make_task(name: str) -> None:
        running.add(name)
        try:
            await asyncio.sleep(1000)
        except asyncio.CancelledError:
            cancelled.add(name)
            raise

    supervisor = Supervisor()
    for name in ("alpha", "beta", "gamma"):
        n = name  # avoid closure capture
        supervisor.register(n, lambda n=n: make_task(n))  # type: ignore[misc]

    task = asyncio.create_task(supervisor.run())
    # Let all tasks start
    for _ in range(6):
        await asyncio.sleep(0)

    assert running == {"alpha", "beta", "gamma"}

    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert cancelled == {"alpha", "beta", "gamma"}


# ---------------------------------------------------------------------------
# Exit policy
# ---------------------------------------------------------------------------


async def test_exit_policy_calls_os_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exit policy must call os._exit(1) when the task fails.

    ``os._exit`` is a C-level call that cannot be caught by pytest.raises.
    Raising SystemExit from the mock also bypasses asyncio's normal exception
    routing (asyncio re-raises KeyboardInterrupt/SystemExit through the event
    loop, causing the test runner itself to exit).  Raise a plain RuntimeError
    instead so the mock unwinds the coroutine through the normal path.
    """
    exit_codes: list[int] = []

    def fake_exit(code: int) -> None:
        exit_codes.append(code)
        raise RuntimeError(f"os._exit({code}) mocked in test")

    monkeypatch.setattr("companion.supervisor.os._exit", fake_exit)

    async def always_crashes() -> None:
        raise RuntimeError("fatal")

    supervisor = Supervisor()
    supervisor.register("fatal", always_crashes, policy=RestartPolicy(mode="exit"))

    with pytest.raises(RuntimeError, match=r"os\._exit"):
        await supervisor.run()

    assert exit_codes == [1]


async def test_restart_policy_mode_restart_does_not_call_os_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restart policy must never call os._exit."""
    exit_called = False

    def fake_exit(code: int) -> None:
        nonlocal exit_called
        exit_called = True

    monkeypatch.setattr("companion.supervisor.os._exit", fake_exit)

    stable = asyncio.Event()
    starts: list[int] = []

    async def crashes_once() -> None:
        starts.append(1)
        if len(starts) == 1:
            raise RuntimeError("first crash")
        stable.set()
        await asyncio.get_running_loop().create_future()

    supervisor = Supervisor()
    supervisor.register(
        "crasher", crashes_once, policy=RestartPolicy(mode="restart", initial_delay=0.0)
    )

    task = asyncio.create_task(supervisor.run())
    await asyncio.wait_for(stable.wait(), timeout=2.0)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert not exit_called
