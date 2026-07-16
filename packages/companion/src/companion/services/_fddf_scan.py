"""Subprocess helper: one LE scan window watching for the speaker's FDDF advert.

Running in a subprocess avoids interaction between bleak's dbus-fast MessageBus
(held in the companion asyncio loop) and any BlueZ calls made from that loop —
the same isolation rationale as :mod:`companion.services._a2dp_connect`.

Usage::

    python -m companion.services._fddf_scan <BREDR_MAC> [window_seconds]

Output protocol (single line on stdout):
  "hex:<payload-hex>"  — a fresh FDDF payload whose embedded BR/EDR address
                         matches <BREDR_MAC> was received during the window
  "none"               — no matching advertisement within the window
  "err:<detail>"       — the scan could not run

**Freshness matters.** BlueZ caches a device's last-seen ``ServiceData``
indefinitely, so reading the property outside a discovery window can return
state that is hours old (observed in practice — see
docs/reverse-engineering/protocol.md § "FDDF Advertisement"). This helper
therefore only reports payloads observed *during* its own discovery window: a
``PropertiesChanged`` carrying ``ServiceData``, an ``InterfacesAdded`` for a
newly seen device, or — for the payload-unchanged case, where BlueZ emits no
``ServiceData`` signal — a ``PropertiesChanged`` carrying ``RSSI``, which
proves the device is advertising right now and hence that the cached
``ServiceData`` it accompanies is current.
"""

from __future__ import annotations

import asyncio
import sys

_BLUEZ = "org.bluez"
_ADAPTER = "/org/bluez/hci0"
_DEFAULT_WINDOW = 12.0


async def _scan(target_mac: str, window: float) -> None:
    from dbus_fast import BusType, DBusError, Variant
    from dbus_fast.aio import MessageBus
    from dbus_fast.aio.proxy_object import ProxyInterface

    from companion.services.bluez_dbus import HARMAN_FDDF_UUID, extract_bredr_address

    target = target_mac.upper()
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    found: asyncio.Future[bytes] = asyncio.get_event_loop().create_future()
    read_tasks: set[asyncio.Task[None]] = set()

    def _try_payload(service_data: dict[str, object]) -> None:
        if found.done():
            return
        raw = service_data.get(HARMAN_FDDF_UUID)
        if raw is None:
            return
        if isinstance(raw, Variant):
            raw = raw.value
        if not isinstance(raw, (bytes, bytearray)):
            return
        payload = bytes(raw)
        if extract_bredr_address(payload) == target:
            found.set_result(payload)

    async def _proxy_interface(path: str, interface: str) -> ProxyInterface:
        introspection = await bus.introspect(_BLUEZ, path)
        obj = bus.get_proxy_object(_BLUEZ, path, introspection)
        return obj.get_interface(interface)

    async def _read_current_service_data(path: str) -> None:
        """Fetch ServiceData for a device that just proved it is advertising."""
        try:
            device = await _proxy_interface(path, "org.bluez.Device1")
            service_data = await device.get_service_data(  # type: ignore[attr-defined]
                unpack_variants=True
            )
        except DBusError:
            return
        if service_data:
            _try_payload(service_data)

    async def _watch_device(path: str) -> None:
        try:
            props = await _proxy_interface(path, "org.freedesktop.DBus.Properties")
        except DBusError:
            return

        def on_changed(_iface: str, changed: dict[str, Variant], _invalid: list[str]) -> None:
            if "ServiceData" in changed:
                _try_payload(changed["ServiceData"].value)
            elif "RSSI" in changed and not found.done():
                task = asyncio.ensure_future(_read_current_service_data(path))
                read_tasks.add(task)
                task.add_done_callback(read_tasks.discard)

        props.on_properties_changed(on_changed)  # type: ignore[attr-defined]

    def on_interfaces_added(path: str, interfaces: dict[str, dict[str, Variant]]) -> None:
        if "org.bluez.Device1" not in interfaces:
            return
        service_data_variant = interfaces["org.bluez.Device1"].get("ServiceData")
        if service_data_variant is not None:
            _try_payload(service_data_variant.value)
        task = asyncio.ensure_future(_watch_device(path))
        read_tasks.add(task)
        task.add_done_callback(read_tasks.discard)

    try:
        obj_manager = await _proxy_interface("/", "org.freedesktop.DBus.ObjectManager")
        obj_manager.on_interfaces_added(on_interfaces_added)  # type: ignore[attr-defined]
        existing = await obj_manager.call_get_managed_objects()  # type: ignore[attr-defined]
        for path, interfaces in existing.items():
            if "org.bluez.Device1" in interfaces:
                await _watch_device(path)

        adapter = await _proxy_interface(_ADAPTER, "org.bluez.Adapter1")
        await adapter.call_set_discovery_filter(  # type: ignore[attr-defined]
            {"Transport": Variant("s", "le"), "DuplicateData": Variant("b", True)}
        )
        try:
            await adapter.call_start_discovery()  # type: ignore[attr-defined]
        except DBusError as exc:
            if "InProgress" not in (exc.type or ""):
                raise
        try:
            payload = await asyncio.wait_for(found, timeout=window)
            print(f"hex:{payload.hex()}", flush=True)
        except TimeoutError:
            print("none", flush=True)
        finally:
            try:
                await adapter.call_stop_discovery()  # type: ignore[attr-defined]
            except DBusError:
                pass
    except Exception as exc:
        print(f"err:{exc}", flush=True)
    finally:
        for task in read_tasks:
            task.cancel()
        bus.disconnect()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("err:usage: _fddf_scan.py <BREDR_MAC> [window_seconds]", flush=True)
        sys.exit(1)
    mac = sys.argv[1]
    window_arg = float(sys.argv[2]) if len(sys.argv) > 2 else _DEFAULT_WINDOW
    asyncio.run(_scan(mac, window_arg))
