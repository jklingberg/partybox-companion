"""Subprocess helper: manage A2DP profile for a given MAC address.

Running in a subprocess avoids interaction between bleak's dbus-fast MessageBus
(held in the companion asyncio loop) and any BlueZ calls made from that loop.

Commands (argv[2]):
  (none)   — connect A2DP profile; prints "ok" or "err:<message>"
  check    — check for an established A2DP Sink MediaTransport1 on the
             device; prints "true" or "false"
"""

from __future__ import annotations

import asyncio
import sys

_A2DP_SINK_UUID = "0000110b-0000-1000-8000-00805f9b34fb"
_A2DP_SOURCE_UUID = "0000110a-0000-1000-8000-00805f9b34fb"
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
        introspection = await bus.introspect(_BLUEZ, "/")
        obj = bus.get_proxy_object(_BLUEZ, "/", introspection)
        manager: ProxyInterface = obj.get_interface("org.freedesktop.DBus.ObjectManager")
        objects = await asyncio.wait_for(
            manager.call_get_managed_objects(),  # type: ignore[attr-defined]
            timeout=5,
        )
        device_prefix = _device_path(address) + "/"
        for object_path, interfaces in objects.items():
            transport = interfaces.get("org.bluez.MediaTransport1")
            if transport is None or not object_path.startswith(device_prefix):
                continue
            uuid = transport["UUID"].value.lower()
            # Transport UUID reflects the LOCAL role: Source (0x110a) when Pi sends
            # audio to speaker, Sink (0x110b) if Pi were receiving.  We are always
            # the source, so check for Source UUID.
            if uuid == _A2DP_SOURCE_UUID or uuid == _A2DP_SINK_UUID:
                print("true", flush=True)
                return
        print("false", flush=True)
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
