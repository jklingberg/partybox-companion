"""Minimal async systemd-logind D-Bus client for appliance power-off.

Talks to ``org.freedesktop.login1`` over the system bus via ``dbus-fast`` —
the same backend ``bluez_dbus.py`` already uses for BlueZ (see that module's
docstring for the general pattern this mirrors). This exists solely to let
the idle-battery-shutdown watcher (ADR-038) power off the Pi: ``companion``
runs with ``NoNewPrivileges=true`` (see ``system/systemd/companion.service``),
which blocks a raw ``sudo poweroff`` subprocess call outright — ``sudo``
needs the setuid escalation that flag exists to prevent. Going through
logind over D-Bus, authorized by a narrow polkit rule for the ``companion``
user (installed by ``install.sh``, see ADR-038), sidesteps that entirely: no
Linux capability or setuid path is involved, so ``NoNewPrivileges`` doesn't
apply.

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

_LOGIND_BUS_NAME = "org.freedesktop.login1"
_LOGIND_PATH = "/org/freedesktop/login1"
_LOGIND_MANAGER_INTERFACE = "org.freedesktop.login1.Manager"


async def _call(interface: ProxyInterface, method_name: str, *args: object) -> Any:  # noqa: ANN401
    """Invoke a D-Bus method on a dynamically-generated proxy interface.

    See ``bluez_dbus._call`` — same reasoning, duplicated rather than shared
    since these two modules talk to unrelated D-Bus services and neither
    should depend on the other.

    ``dbus-fast`` snake_cases the D-Bus method name for this generated
    attribute regardless of its actual PascalCase wire name — confirmed the
    hard way on real hardware: ``Manager.PowerOff`` is reachable only as
    ``call_power_off``, not ``call_PowerOff``. Pass *method_name* already
    snake_cased.
    """
    fn = cast(Callable[..., Awaitable[Any]], getattr(interface, f"call_{method_name}"))
    return await fn(*args)


async def power_off() -> None:
    """Ask systemd-logind to power off the host.

    A single one-shot D-Bus call — connects, calls ``Manager.PowerOff``,
    disconnects. ``interactive=False`` so logind fails outright rather than
    prompting (there is no session to prompt; authorization comes entirely
    from the polkit rule for the ``companion`` user).
    """
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        introspection = await bus.introspect(_LOGIND_BUS_NAME, _LOGIND_PATH)
        proxy = bus.get_proxy_object(_LOGIND_BUS_NAME, _LOGIND_PATH, introspection)
        manager = proxy.get_interface(_LOGIND_MANAGER_INTERFACE)
        await _call(manager, "power_off", False)
    finally:
        bus.disconnect()
