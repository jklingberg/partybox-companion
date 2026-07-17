"""Subprocess helper: power-cycle the Bluetooth adapter via BlueZ D-Bus.

Running in a subprocess avoids interaction between bleak's dbus-fast
MessageBus (held in the companion asyncio loop) and any BlueZ calls made from
that loop — the same isolation rationale as :mod:`companion.services._a2dp_connect`.

Used by the DeviceManager's wedge recovery (ADR-039): when scanning works but
GATT connections fail repeatedly, powering the adapter off and on clears the
degraded controller state that otherwise persists until a service restart.

**The worst possible outcome is exiting with the adapter still powered off** —
the manager cannot scan then, and nothing else re-powers the adapter until a
bluetoothd restart or reboot. Three defenses:

- The power-on runs in a ``finally``, so any failure after the power-off
  (including cancellation) still attempts it, with one retry.
- SIGTERM (systemd stops the service by signalling the whole cgroup, so a
  recovery in flight during shutdown receives it) cancels the task instead of
  killing the process, which lets that ``finally`` run.
- Each D-Bus write is individually bounded, so a lost reply on the power-off
  becomes an exception (→ finally) rather than a hang until the parent's
  kill-timeout (which would strand whatever state the adapter was in).

Usage::

    python -m companion.services._adapter_reset [settle_seconds]

Output protocol (single line on stdout):
  "ok"           — the adapter was powered off and back on
  "err:<detail>" — the power-cycle could not be completed
"""

from __future__ import annotations

import asyncio
import signal
import sys

_BLUEZ = "org.bluez"
_ADAPTER = "/org/bluez/hci0"
_DEFAULT_SETTLE = 1.0
# Ceiling per Powered write. bluetoothd answers these in well under a second;
# a lost reply must surface as an error while the parent (20 s budget) is
# still listening.
_SET_TIMEOUT = 5.0


async def _reset(settle: float) -> None:
    from dbus_fast import BusType, Variant
    from dbus_fast.aio import MessageBus

    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None
    loop.add_signal_handler(signal.SIGTERM, task.cancel)

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        introspection = await bus.introspect(_BLUEZ, _ADAPTER)
        obj = bus.get_proxy_object(_BLUEZ, _ADAPTER, introspection)
        props = obj.get_interface("org.freedesktop.DBus.Properties")

        async def set_powered(value: bool) -> None:
            await asyncio.wait_for(
                props.call_set(  # type: ignore[attr-defined]
                    "org.bluez.Adapter1", "Powered", Variant("b", value)
                ),
                timeout=_SET_TIMEOUT,
            )

        await set_powered(False)
        try:
            await asyncio.sleep(settle)
        finally:
            # Runs on success, error, and cancellation alike. One retry:
            # bluetoothd can be momentarily busy right after the power-off.
            try:
                await set_powered(True)
            except Exception:
                await asyncio.sleep(1.0)
                await set_powered(True)
        print("ok", flush=True)
    except Exception as exc:
        # CancelledError is BaseException and passes through: a terminated
        # run prints nothing, and the parent (if still alive) reports False.
        print(f"err:{exc}", flush=True)
    finally:
        bus.disconnect()


if __name__ == "__main__":
    settle_arg = float(sys.argv[1]) if len(sys.argv) > 1 else _DEFAULT_SETTLE
    asyncio.run(_reset(settle_arg))
