"""Subprocess helper: manage A2DP profile for a given MAC address.

Running in a subprocess avoids interaction between bleak's dbus-fast MessageBus
(held in the companion asyncio loop) and any BlueZ calls made from that loop.

Output protocol (single line on stdout):
  connect:  "ok"                       — profile connected
            "err:<CODE>:<detail>"      — classified failure; <CODE> is the
                                         machine contract, <detail> is human text
            "err:<detail>"             — unclassified failure (free text only)
  check:    "true" | "false"

The first colon-delimited token after ``err:`` is the machine-readable status
code and is the *only* thing the parent should branch on — see
:func:`error_code`. Human-readable detail after the code is diagnostic only, so
its wording can change without breaking the parent. Known codes live in
:data:`_STATUS_CODES`; add one there before the parent may rely on it.
"""

from __future__ import annotations

import asyncio
import sys

_A2DP_SINK_UUID = "0000110b-0000-1000-8000-00805f9b34fb"
_A2DP_SOURCE_UUID = "0000110a-0000-1000-8000-00805f9b34fb"
_BLUEZ = "org.bluez"
_ADAPTER = "/org/bluez/hci0"

# Machine-readable status code emitted as `err:STALE_BOND:<detail>` when BlueZ
# has no Device1 object for the sink address — i.e. the bond is stale (removed
# out from under us). The parent (AudioService) matches this CODE, not the
# human-readable detail, and uses it to skip the pointless post-failure
# disconnect (which would introspect the absent device and emit a dbus_fast
# add-match ERROR).
STALE_BOND_CODE = "STALE_BOND"

# The complete set of machine-readable status codes the helper may emit. The
# parser only recognises a token as a code if it is in this set, so free-text
# detail that happens to contain a colon is never mistaken for a code.
_STATUS_CODES = frozenset({STALE_BOND_CODE})


def error_code(line: str) -> str | None:
    """Return the machine-readable status code from a helper output line.

    ``line`` is a raw stdout line from this helper. Returns the ``<CODE>`` token
    for a classified ``err:<CODE>:<detail>`` line, or ``None`` for ``ok``,
    ``true``/``false``, or an unclassified ``err:<detail>`` line. This is the
    single source of truth for the subprocess error protocol — both this module
    (which emits the codes) and the parent (which branches on them) share it, so
    the parent is coupled to a code constant, not to message wording.
    """
    if not line.startswith("err:"):
        return None
    token = line[len("err:") :].split(":", 1)[0]
    return token if token in _STATUS_CODES else None


def _device_path(address: str) -> str:
    return f"{_ADAPTER}/dev_{address.replace(':', '_').upper()}"


async def _connect(address: str) -> None:
    from dbus_fast import BusType, DBusError
    from dbus_fast.aio import MessageBus
    from dbus_fast.aio.proxy_object import ProxyInterface
    from dbus_fast.errors import InterfaceNotFoundError

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    path = _device_path(address)
    # Introspection, proxy construction and get_interface() must stay INSIDE the
    # try: when the bond has been removed (user re-paired the speaker with
    # another device, or `bluetoothctl remove`) BlueZ no longer exports a
    # Device1 object at this path, so get_interface() raises
    # InterfaceNotFoundError.  Left uncaught it crashes the helper and dumps a
    # traceback into the retry-loop's WARNING line (FAULT-05).
    try:
        introspection = await bus.introspect(_BLUEZ, path)
        obj = bus.get_proxy_object(_BLUEZ, path, introspection)
        device: ProxyInterface = obj.get_interface("org.bluez.Device1")
        await asyncio.wait_for(
            device.call_connect_profile(_A2DP_SINK_UUID),  # type: ignore[attr-defined]
            timeout=30,
        )
        print("ok", flush=True)
    except InterfaceNotFoundError:
        # No Device1 at this path — the bond is stale (removed out from under
        # us).  Emit the machine-readable code plus human detail instead of a
        # traceback; the retry loop keeps trying and POST /audio/pair recovers.
        print(f"err:{STALE_BOND_CODE}:device unknown to BlueZ (re-pair required)", flush=True)
    except DBusError as exc:
        # exc.text can be empty (seen on hardware while the speaker was
        # unplugged) — fall back to the D-Bus error name so the retry-loop
        # log line is never blank.
        print(f"err:{exc.text or exc.type or exc!r}", flush=True)
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
