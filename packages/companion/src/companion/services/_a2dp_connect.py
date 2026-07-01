"""Subprocess helper: manage A2DP profile for a given MAC address.

Running in a subprocess avoids the dbus-fast / bleak event-loop routing
conflict: bleak holds its own MessageBus in the companion asyncio loop, and a
second MessageBus in the same loop misroutes D-Bus responses.

Commands (argv[2]):
  (none)   — connect A2DP profile; prints "ok" or "err:<message>"
  check    — check Device1.Connected; prints "true" or "false"
"""

from __future__ import annotations

import asyncio
import sys

_A2DP_SINK_UUID = "0000110b-0000-1000-8000-00805f9b34fb"
_BLUEZ = "org.bluez"
_ADAPTER = "/org/bluez/hci0"


def _device_path(address: str) -> str:
    return f"{_ADAPTER}/dev_{address.replace(':', '_').upper()}"


async def _connect(address: str) -> None:
    from dbus_fast import BusType, DBusError
    from dbus_fast.aio import MessageBus
    from dbus_fast.aio.proxy_object import ProxyInterface

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    path = _device_path(address)
    introspection = await bus.introspect(_BLUEZ, path)
    obj = bus.get_proxy_object(_BLUEZ, path, introspection)
    device: ProxyInterface = obj.get_interface("org.bluez.Device1")
    try:
        await asyncio.wait_for(
            device.call_connect_profile(_A2DP_SINK_UUID),  # type: ignore[attr-defined]
            timeout=30,
        )
        print("ok", flush=True)
    except DBusError as exc:
        print(f"err:{exc.text}", flush=True)
    except Exception as exc:
        print(f"err:{exc}", flush=True)
    finally:
        bus.disconnect()


async def _check(address: str) -> None:
    from dbus_fast import BusType
    from dbus_fast.aio import MessageBus
    from dbus_fast.aio.proxy_object import ProxyInterface

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        path = _device_path(address)
        introspection = await bus.introspect(_BLUEZ, path)
        obj = bus.get_proxy_object(_BLUEZ, path, introspection)
        device: ProxyInterface = obj.get_interface("org.bluez.Device1")
        connected = await asyncio.wait_for(
            device.get_connected(unpack_variants=True),  # type: ignore[attr-defined]
            timeout=5,
        )
        print("true" if connected else "false", flush=True)
    except Exception:
        print("false", flush=True)
    finally:
        bus.disconnect()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("err:usage: _a2dp_connect.py <MAC> [check]", flush=True)
        sys.exit(1)
    address = sys.argv[1]
    command = sys.argv[2] if len(sys.argv) > 2 else "connect"
    if command == "check":
        asyncio.run(_check(address))
    else:
        asyncio.run(_connect(address))
