"""Minimal async systemd D-Bus client for starting a single named unit.

Talks to ``org.freedesktop.systemd1`` over the system bus via ``dbus-fast`` —
the same backend ``bluez_dbus.py`` and ``login1_dbus.py`` already use (see
``login1_dbus.py``'s docstring for the general pattern this mirrors). This
exists solely to let ``SshAccessService`` (ADR-042) trigger the root-owned
``companion-ssh-apply.service`` oneshot unit: ``companion`` runs with
``NoNewPrivileges=true`` and no sudoers grant, which rules out a raw
``sudo systemctl start ...`` subprocess call outright. Going through
``systemd1`` over D-Bus, authorized by a polkit rule scoped to exactly that
one unit name (installed by ``install.sh``, see ADR-042), sidesteps that
entirely: no Linux capability or setuid path is involved.

This module deliberately does *not* use ``from __future__ import
annotations`` — see ``bluez_dbus.py``'s docstring for why: under PEP 563,
``dbus-fast``'s ``Annotated[...]``-based D-Bus method signature inference
breaks silently.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

from dbus_fast import BusType
from dbus_fast.aio import MessageBus
from dbus_fast.aio.proxy_object import ProxyInterface

log = logging.getLogger(__name__)

_SYSTEMD_BUS_NAME = "org.freedesktop.systemd1"
_SYSTEMD_PATH = "/org/freedesktop/systemd1"
_SYSTEMD_MANAGER_INTERFACE = "org.freedesktop.systemd1.Manager"
_JOB_COMPLETION_TIMEOUT = 30.0


async def _call(interface: ProxyInterface, method_name: str, *args: object) -> Any:  # noqa: ANN401
    """Invoke a D-Bus method on a dynamically-generated proxy interface.

    See ``login1_dbus._call`` — same reasoning, duplicated rather than
    shared since these two modules talk to unrelated D-Bus services and
    neither should depend on the other.
    """
    fn = cast(Callable[..., Awaitable[Any]], getattr(interface, f"call_{method_name}"))
    return await fn(*args)


async def start_unit(
    unit_name: str, mode: str = "replace", *, timeout: float = _JOB_COMPLETION_TIMEOUT
) -> None:
    """Ask systemd to start *unit_name* and wait for that job to finish.

    Connects, calls ``Manager.StartUnit``, then waits for the matching
    ``JobRemoved`` signal before disconnecting — it does *not* return as
    soon as the job is merely queued.

    This matters specifically because ``mode="replace"`` only preempts a
    *queued* job, not one already executing: if this function returned as
    soon as ``StartUnit`` handed back a job path (true fire-and-forget), a
    second call arriving while ``companion-ssh-apply.service``'s previous
    run is still executing would have its ``StartUnit`` request *merged*
    into that already-running job by systemd rather than starting a fresh
    run — silently dropping whatever new desired state the second caller
    just wrote to disk, with no error raised anywhere. Waiting for
    ``JobRemoved`` means each call only returns once its own request has
    genuinely been fully processed, so by the time it returns, any following
    call is guaranteed to start a brand new job against the true current
    state. ``companion-ssh-apply.service`` itself completes in well under a
    second (see ``companion-ssh-apply.sh``), so this is a short wait in
    practice — bounded by *timeout* as a backstop, not a long-poll.

    As a side effect, this also means callers (e.g. the SSH settings REST
    endpoint) can read back application state immediately after this
    returns and get the fresh, post-apply result rather than a stale one.

    *mode* is passed through to systemd unchanged; ``"replace"`` (systemd's
    own default for `systemctl start`) queues the job, replacing any
    conflicting *queued* job for the same unit.
    """
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        introspection = await bus.introspect(_SYSTEMD_BUS_NAME, _SYSTEMD_PATH)
        proxy = bus.get_proxy_object(_SYSTEMD_BUS_NAME, _SYSTEMD_PATH, introspection)
        manager = proxy.get_interface(_SYSTEMD_MANAGER_INTERFACE)

        job_path = await _call(manager, "start_unit", unit_name, mode)

        loop = asyncio.get_running_loop()
        job_done: asyncio.Future[None] = loop.create_future()

        # dbus-fast auto-generates on_<signal_name>/off_<signal_name> for a
        # proxy interface's signals, mirroring the call_<method_name>
        # generation _call() already relies on above.
        def _on_job_removed(_job_id: int, job: str, _unit: str, _result: str) -> None:
            if job == job_path and not job_done.done():
                job_done.set_result(None)

        manager.on_job_removed(_on_job_removed)  # type: ignore[attr-defined]
        try:
            await asyncio.wait_for(job_done, timeout=timeout)
        finally:
            manager.off_job_removed(_on_job_removed)  # type: ignore[attr-defined]
    finally:
        bus.disconnect()
