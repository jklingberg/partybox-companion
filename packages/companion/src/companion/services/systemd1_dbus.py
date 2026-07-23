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


async def _call(interface: ProxyInterface, method_name: str, *args: object) -> Any:  # noqa: ANN401
    """Invoke a D-Bus method on a dynamically-generated proxy interface.

    See ``login1_dbus._call`` — same reasoning, duplicated rather than
    shared since these two modules talk to unrelated D-Bus services and
    neither should depend on the other.
    """
    fn = cast(Callable[..., Awaitable[Any]], getattr(interface, f"call_{method_name}"))
    return await fn(*args)


async def start_unit(unit_name: str, mode: str = "replace") -> None:
    """Ask systemd to start *unit_name*.

    A single one-shot D-Bus call — connects, calls ``Manager.StartUnit``,
    disconnects. Fire-and-forget: this returns once the job is *queued*, not
    once the unit finishes running (``StartUnit`` itself only returns a job
    object path). Callers that need the result (e.g. the Portal reflecting
    an applied SSH setting) poll application-level state written by the unit
    itself, the same pattern already used for WiFi provisioning.

    *mode* is passed through to systemd unchanged; ``"replace"`` (systemd's
    own default for `systemctl start`) queues the job, replacing any
    conflicting queued job for the same unit.
    """
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        introspection = await bus.introspect(_SYSTEMD_BUS_NAME, _SYSTEMD_PATH)
        proxy = bus.get_proxy_object(_SYSTEMD_BUS_NAME, _SYSTEMD_PATH, introspection)
        manager = proxy.get_interface(_SYSTEMD_MANAGER_INTERFACE)
        await _call(manager, "start_unit", unit_name, mode)
    finally:
        bus.disconnect()
