"""Subprocess helper: power-cycle the Bluetooth adapter via BlueZ D-Bus.

Running in a subprocess avoids interaction between bleak's dbus-fast
MessageBus (held in the companion asyncio loop) and any BlueZ calls made from
that loop — the same isolation rationale as :mod:`companion.services._a2dp_connect`.

Used by the DeviceManager's wedge recovery (ADR-039): when scanning works but
GATT connections fail repeatedly, powering the adapter off and on clears the
degraded controller state that otherwise persists until a service restart.

Usage::

    python -m companion.services._adapter_reset [settle_seconds]

Output protocol (single line on stdout):
  "ok"           — the adapter was powered off and back on
  "err:<detail>" — the power-cycle could not be completed
"""

from __future__ import annotations

import asyncio
import sys

_BLUEZ = "org.bluez"
_ADAPTER = "/org/bluez/hci0"
_DEFAULT_SETTLE = 1.0


async def _reset(settle: float) -> None:
    from dbus_fast import BusType, Variant
    from dbus_fast.aio import MessageBus

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        introspection = await bus.introspect(_BLUEZ, _ADAPTER)
        obj = bus.get_proxy_object(_BLUEZ, _ADAPTER, introspection)
        props = obj.get_interface("org.freedesktop.DBus.Properties")
        await props.call_set(  # type: ignore[attr-defined]
            "org.bluez.Adapter1", "Powered", Variant("b", False)
        )
        await asyncio.sleep(settle)
        await props.call_set(  # type: ignore[attr-defined]
            "org.bluez.Adapter1", "Powered", Variant("b", True)
        )
        print("ok", flush=True)
    except Exception as exc:
        print(f"err:{exc}", flush=True)
    finally:
        bus.disconnect()


if __name__ == "__main__":
    settle_arg = float(sys.argv[1]) if len(sys.argv) > 1 else _DEFAULT_SETTLE
    asyncio.run(_reset(settle_arg))
