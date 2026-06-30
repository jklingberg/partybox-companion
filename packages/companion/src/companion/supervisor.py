"""Centralised supervision of long-running asyncio tasks.

Every background task in the appliance — DeviceManager, SpotifyService,
AudioService, and so on — is registered here.  If a task exits unexpectedly
(by exception or by returning prematurely), the supervisor restarts it after
an exponential backoff delay instead of letting the failure disappear silently.

Usage::

    supervisor = Supervisor()
    supervisor.register("device-manager", manager.run)
    supervisor.register("spotify-service", spotify.run,
                        policy=RestartPolicy(initial_delay=5.0))

    supervisor_task = asyncio.create_task(supervisor.run(), name="supervisor")
    try:
        await some_blocking_work()
    finally:
        supervisor_task.cancel()
        with suppress(asyncio.CancelledError):
            await supervisor_task

The supervisor coordinates task lifecycle and restart policy; individual
services remain responsible only for their own domain logic.  See ADR-024.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Literal

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RestartPolicy:
    """Defines how the supervisor recovers from an unexpected task exit.

    Args:
        mode: ``"restart"`` re-launches the factory after a backoff delay.
            ``"exit"`` calls ``os._exit(1)`` so that systemd can perform a
            clean appliance restart — use this for tasks whose failure makes
            the appliance non-functional in a way that cannot be recovered
            in-process.
        initial_delay: seconds before the first restart attempt.
        max_delay: upper bound on the restart delay after many failures.
        backoff_factor: multiplier applied to the delay on each consecutive
            failure.
        reset_after: if a task runs for at least this many seconds before
            exiting, the backoff resets to *initial_delay* on the next
            restart — the task is considered to have been "stable".
    """

    mode: Literal["restart", "exit"] = "restart"
    initial_delay: float = 5.0
    max_delay: float = 300.0
    backoff_factor: float = 2.0
    reset_after: float = 60.0


_DEFAULT_RESTART_POLICY = RestartPolicy()


@dataclass(frozen=True)
class TaskHealth:
    """Point-in-time snapshot of one supervised task's health.

    Returned by :meth:`Supervisor.health`.  All timestamps are
    :func:`time.monotonic` values and are therefore not comparable across
    process restarts.

    Future milestones will route this through ``/api/v1/health`` and Portal
    diagnostics without requiring changes to the Supervisor architecture.
    """

    name: str
    state: Literal["waiting", "running"]
    running_since: float | None
    """Monotonic timestamp when the current run started; ``None`` when waiting."""
    last_failure_at: float | None
    """Monotonic timestamp of the most recent unexpected exit; ``None`` if the
    task has never failed."""
    last_exception: Exception | None
    """Most recent crash exception; ``None`` if the last exit was a clean return
    (unexpected but without an exception) or if the task has never failed."""
    total_failures: int
    """Cumulative count of unexpected exits; never resets."""


@dataclass
class _Entry:
    """Internal mutable state for one supervised task."""

    name: str
    factory: Callable[[], Coroutine[Any, Any, None]]
    policy: RestartPolicy
    # Backoff state — mutable across restarts; reset_after logic lives in
    # consume_delay() so _supervise stays readable.
    delay: float = field(default=0.0, init=False)
    restart_count: int = field(default=0, init=False)
    # Health state — populated by _supervise; exposed via Supervisor.health().
    state: Literal["waiting", "running"] = field(default="waiting", init=False)
    running_since: float | None = field(default=None, init=False)
    last_failure_at: float | None = field(default=None, init=False)
    last_exception: Exception | None = field(default=None, init=False)
    total_failures: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.delay = self.policy.initial_delay

    def consume_delay(self, runtime: float) -> float:
        """Return the sleep duration for this restart and advance backoff state.

        If *runtime* meets or exceeds *reset_after*, the delay is reset to
        *initial_delay* before computing the return value, reflecting that the
        previous run was stable.  The internal delay is then advanced so that
        the next call returns a larger value.
        """
        if runtime >= self.policy.reset_after:
            self.delay = self.policy.initial_delay
            self.restart_count = 0
        delay = self.delay
        self.delay = min(delay * self.policy.backoff_factor, self.policy.max_delay)
        self.restart_count += 1
        return delay


class Supervisor:
    """Coordinates the lifecycle of long-running asyncio tasks.

    Services remain responsible for their own domain logic; the supervisor is
    responsible for starting them, detecting unexpected exits, and restarting
    them according to a configurable policy.

    Instantiate once at appliance startup, register every background task,
    then call :meth:`run` as an asyncio task.  Cancellation cascades cleanly
    to all supervised tasks.
    """

    def __init__(self) -> None:
        self._entries: list[_Entry] = []

    def register(
        self,
        name: str,
        factory: Callable[[], Coroutine[Any, Any, None]],
        *,
        policy: RestartPolicy = _DEFAULT_RESTART_POLICY,
    ) -> None:
        """Register a long-running task with the supervisor.

        Args:
            name: human-readable label used in log messages and diagnostics.
            factory: zero-argument callable that returns a *new* coroutine on
                each call.  It must be callable multiple times — bound methods,
                lambdas, and ``functools.partial`` all work; a bare coroutine
                object does not.
            policy: restart strategy.  Defaults to ``RestartPolicy()``
                (restart mode, 5 s initial delay, 5-minute cap, x2 backoff,
                reset after 60 s of stable runtime).
        """
        self._entries.append(_Entry(name=name, factory=factory, policy=policy))

    def health(self) -> list[TaskHealth]:
        """Return a point-in-time snapshot of every registered task's health.

        The list preserves registration order.  Safe to call from any asyncio
        task — reads are non-blocking and do not modify state.

        Intended for M17.2 Portal diagnostics and ``/api/v1/health``.
        """
        return [
            TaskHealth(
                name=e.name,
                state=e.state,
                running_since=e.running_since,
                last_failure_at=e.last_failure_at,
                last_exception=e.last_exception,
                total_failures=e.total_failures,
            )
            for e in self._entries
        ]

    async def run(self) -> None:
        """Start all registered tasks and coordinate them until cancelled.

        Each task runs in its own monitor loop; failures are independent.
        Cancellation cascades: when this coroutine is cancelled, every
        supervised task receives a cancellation and the supervisor waits for
        each to finish before returning.
        """
        if not self._entries:
            log.warning("supervisor started with no registered tasks")
            return

        monitor_tasks = [
            asyncio.create_task(self._supervise(entry), name=f"supervisor:{entry.name}")
            for entry in self._entries
        ]
        try:
            await asyncio.gather(*monitor_tasks)
        except BaseException:
            for t in monitor_tasks:
                t.cancel()
            for t in monitor_tasks:
                with suppress(BaseException):
                    await t
            raise

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _supervise(self, entry: _Entry) -> None:
        """Coordinate one entry's lifecycle, restarting it after every unexpected exit.

        Runs the factory coroutine directly (not as a sub-task) so that
        CancelledError propagates from the supervisor into the factory without
        any additional machinery.  The factory's own cleanup (e.g.
        ``DeviceManager._disconnect()``) runs naturally before the coroutine
        re-raises CancelledError.

        Health state (``entry.state``, ``entry.running_since``, etc.) is
        updated synchronously between yield points, so any concurrent reader
        sees a consistent snapshot.
        """
        log.debug("supervisor: starting %s", entry.name)
        while True:
            started = time.monotonic()
            entry.state = "running"
            entry.running_since = started
            try:
                await entry.factory()
            except asyncio.CancelledError:
                entry.state = "waiting"
                entry.running_since = None
                raise
            except Exception as exc:
                log.error(
                    "supervisor: %s crashed (%s: %s)",
                    entry.name,
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
                entry.last_exception = exc
            else:
                # A long-running task should never return None; treat a clean
                # return as an unexpected failure.
                log.error(
                    "supervisor: %s exited unexpectedly without raising",
                    entry.name,
                )
                entry.last_exception = None  # clean exit, not a crash

            # Reached only on unexpected exit (not CancelledError).
            now = time.monotonic()
            entry.state = "waiting"
            entry.running_since = None
            entry.last_failure_at = now
            entry.total_failures += 1
            runtime = now - started

            if entry.policy.mode == "exit":
                log.critical(
                    "supervisor: %s failed after %.1fs (policy=exit) — "
                    "terminating process for systemd restart",
                    entry.name,
                    runtime,
                )
                os._exit(1)

            delay = entry.consume_delay(runtime)
            log.info(
                "supervisor: %s will restart in %.1fs (restart #%d)",
                entry.name,
                delay,
                entry.restart_count,
            )
            await asyncio.sleep(delay)
