"""Subprocess helper: disconnect stale LE connections to the speaker.

Running in a subprocess avoids interaction between bleak's dbus-fast
MessageBus (held in the companion asyncio loop) and any BlueZ calls made from
that loop — the same isolation rationale as :mod:`companion.services._a2dp_connect`.

Used by the DeviceManager's empty-scan reclaim: if a previous companion
process died without disconnecting its BLE control link (crash, SIGKILL,
power loss), bluetoothd keeps the link up, the speaker stops advertising its
connectable set, and the manager scans forever while the Portal reports the
speaker as off. A connected LE device named like a PartyBox on our own
adapter while the manager holds no connection is that orphan by definition —
another holder (e.g. a phone) never appears on the Pi's adapter.

Only ``AddressType == "random"`` devices are touched: the control plane uses
LE rotating private addresses (ADR-015), while the speaker's A2DP link is a
BR/EDR device with a ``public`` address — disconnecting that one would cut
live audio.

Usage::

    python -m companion.services._le_reclaim

Output protocol (single line on stdout):
  "ok:<n>"       — n stale connections were disconnected (0 = none found)
  "err:<detail>" — enumeration or disconnect failed
"""

from __future__ import annotations

import asyncio
from typing import Any

_BLUEZ = "org.bluez"
_NAME_MARKER = "PartyBox"
# GetManagedObjects is answered from bluetoothd's in-memory state; Disconnect
# on a dead-ish link can take a supervision-timeout-ish while. Both must fail
# loudly while the parent (30 s budget) is still listening.
_ENUMERATE_TIMEOUT = 5.0
_DISCONNECT_TIMEOUT = 15.0


def _prop(device_props: dict[str, Any], key: str) -> object:
    variant = device_props.get(key)
    return variant.value if variant is not None else None


async def _reclaim() -> None:
    from dbus_fast import BusType
    from dbus_fast.aio import MessageBus

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        introspection = await bus.introspect(_BLUEZ, "/")
        obj = bus.get_proxy_object(_BLUEZ, "/", introspection)
        om = obj.get_interface("org.freedesktop.DBus.ObjectManager")
        managed = await asyncio.wait_for(
            om.call_get_managed_objects(),  # type: ignore[attr-defined]
            timeout=_ENUMERATE_TIMEOUT,
        )

        count = 0
        for path, interfaces in managed.items():
            device_props = interfaces.get("org.bluez.Device1")
            if device_props is None:
                continue
            if _prop(device_props, "Connected") is not True:
                continue
            if _prop(device_props, "AddressType") != "random":
                continue  # never touch the BR/EDR A2DP link
            name = _prop(device_props, "Name") or _prop(device_props, "Alias") or ""
            if not isinstance(name, str) or _NAME_MARKER not in name:
                continue
            dev_introspection = await bus.introspect(_BLUEZ, path)
            dev_obj = bus.get_proxy_object(_BLUEZ, path, dev_introspection)
            dev = dev_obj.get_interface("org.bluez.Device1")
            await asyncio.wait_for(
                dev.call_disconnect(),  # type: ignore[attr-defined]
                timeout=_DISCONNECT_TIMEOUT,
            )
            count += 1
        print(f"ok:{count}", flush=True)
    except Exception as exc:
        print(f"err:{exc}", flush=True)
    finally:
        bus.disconnect()


if __name__ == "__main__":
    asyncio.run(_reclaim())
