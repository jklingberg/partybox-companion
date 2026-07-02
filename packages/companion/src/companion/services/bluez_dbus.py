"""Minimal async BlueZ D-Bus client for first-time BR/EDR pairing.

Talks to ``org.bluez`` over the system bus via ``dbus-fast`` — the same
backend ``bleak`` already pulls in for BLE GATT (see ADR-015). This module is
deliberately narrow: it implements only what :mod:`companion.services.pairing`
needs (LE discovery, FDDF service-data extraction, agent registration, and
Pair/Trust/Connect against a known BR/EDR address), not a general BlueZ
binding.

**Design intent — thin abstraction, not a service layer.** ``BluezClient``
is a *transport wrapper*: it owns a D-Bus connection for the duration of a
single ``async with`` block, translates D-Bus calls into named async methods,
and isolates the dynamic proxy-attribute access so callers stay fully typed.
It must not become a service layer. The markers that signal drift are:

- adding a persistent ``start()``/``stop()`` lifecycle (would duplicate the
  Supervisor's role)
- subscribing to D-Bus signals that outlive a single operation (callers
  should decide what state to watch, not this module)
- accumulating state between method calls (``PairingService`` owns state,
  ``BluezClient`` is stateless beyond the bus connection)
- storing discovered devices or making retry/reconnect decisions

If something needs a longer BlueZ lifetime than one ``_do_pair()`` call,
the right place for that is ``AudioService`` or a new service, not here.

See docs/adr/027-bluetooth-bonding-architecture.md for why this replaced a
``bluetoothctl``/``btmgmt`` subprocess implementation: CLI text-scraping has
no access to raw advertisement data (which is where the BR/EDR address
actually lives — see :func:`extract_bredr_address`), and ``btmgmt`` bypasses
``bluetoothd`` entirely, risking its D-Bus state diverging from the kernel's.

``dbus-fast``'s proxy objects expose D-Bus methods/properties/signals as
attributes generated at runtime (``call_<method>``, ``get_<property>``,
``on_<signal>``), which mypy cannot see on the stub class. All such access is
isolated behind :func:`_call`/:func:`_get_property`/:func:`_set_property` so
the rest of this module — and all of ``pairing.py`` — stays fully typed.

This module deliberately does *not* use ``from __future__ import
annotations``: under PEP 563, annotations are stored as unevaluated source
text, which breaks ``dbus-fast``'s ``Annotated[...]``-based D-Bus method
signature inference (it silently produces an invalid signature instead of
raising at decoration time — confirmed empirically). The two genuine forward
references (``BluezClient`` returning its own type, ``_AgentScope`` defined
after its first use) are quoted instead.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Any, cast

from dbus_fast import BusType, DBusError, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.aio.proxy_object import ProxyInterface, ProxyObject
from dbus_fast.annotations import DBusObjectPath, DBusStr, DBusUInt32
from dbus_fast.service import ServiceInterface, method

log = logging.getLogger(__name__)

_BLUEZ = "org.bluez"
_ADAPTER_PATH = "/org/bluez/hci0"
_AGENT_PATH = "/com/partybox/companion/agent"
_AGENT_CAPABILITY = "NoInputNoOutput"

# Harman International proprietary vendor UUID — NOT a Bluetooth SIG or GATT
# standard. UUID 0xfddf is unregistered and belongs to Harman's internal
# device-discovery protocol. Only JBL/Harman devices advertise this structure;
# no other manufacturer's speakers will have it. The payload format (BR/EDR
# public address at bytes 11-16) was derived from a PartyBox 520 btmon capture
# and is not documented in any public Harman specification — see ADR-027 and
# docs/reverse-engineering/. If a future Harman model places the address at a
# different offset, extend the parsing here (perhaps with a model discriminator
# earlier in the payload), not in the calling code.
HARMAN_FDDF_UUID = "0000fddf-0000-1000-8000-00805f9b34fb"
_FDDF_ADDRESS_OFFSET = 11
_FDDF_ADDRESS_LENGTH = 6

_PAIR_TIMEOUT = 60.0
_TRUST_TIMEOUT = 5.0
_CONNECT_TIMEOUT = 30.0


def extract_bredr_address(service_data: bytes) -> str | None:
    """Extract the BR/EDR public address from Harman FDDF service data.

    Returns ``None`` if the payload is shorter than the known address
    offset — a malformed or unrelated advertisement, not this device.
    """
    end = _FDDF_ADDRESS_OFFSET + _FDDF_ADDRESS_LENGTH
    if len(service_data) < end:
        return None
    addr_bytes = service_data[_FDDF_ADDRESS_OFFSET:end]
    return ":".join(f"{b:02X}" for b in addr_bytes)


class PairingFailedError(Exception):
    """A pair/trust/connect D-Bus call failed or timed out."""


class A2dpConnectError(Exception):
    """Device1.Connect() failed."""


class A2dpProfileUnavailableError(A2dpConnectError):
    """Speaker rejected A2DP — recovery window may still be active.

    The speaker drops the A2DP AVDTP session and then needs ~20 s before it
    will accept a new one.  Callers should NOT call disconnect() after this
    error; the ACL link is already torn down by the speaker.
    """


class _PairingAgent(ServiceInterface):
    """``org.bluez.Agent1`` with ``NoInputNoOutput`` capability.

    NoInputNoOutput forces the Just Works SSP association model on both
    sides — neither device has a way to display or confirm a passkey, so
    none of these methods needs to prompt anyone. They exist only because
    BlueZ requires *some* agent to be registered before ``Pair()`` will
    proceed at all; under Just Works none of the interactive callbacks are
    normally invoked.
    """

    def __init__(self) -> None:
        super().__init__("org.bluez.Agent1")

    @method()
    def Release(self) -> None:
        pass

    @method()
    def Cancel(self) -> None:
        pass

    @method()
    def RequestAuthorization(self, device: DBusObjectPath) -> None:
        pass

    @method()
    def AuthorizeService(self, device: DBusObjectPath, uuid: DBusStr) -> None:
        pass

    @method()
    def RequestConfirmation(self, device: DBusObjectPath, passkey: DBusUInt32) -> None:
        pass


async def _call(interface: ProxyInterface, method_name: str, *args: object) -> Any:  # noqa: ANN401
    """Invoke a D-Bus method on a dynamically-generated proxy interface.

    ``dbus-fast`` attaches ``call_<method>`` at runtime based on the
    interface's introspection data, so it is invisible to mypy on the
    ``ProxyInterface`` stub. This is the single place that crosses that
    boundary.
    """
    fn = cast(Callable[..., Awaitable[Any]], getattr(interface, f"call_{method_name}"))
    return await fn(*args)


async def _get_property(interface: ProxyInterface, name: str) -> Any:  # noqa: ANN401
    fn = cast(Callable[..., Awaitable[Any]], getattr(interface, f"get_{name}"))
    return await fn(unpack_variants=True)


async def _set_property(interface: ProxyInterface, name: str, value: object) -> None:
    fn = cast(Callable[[object], Awaitable[None]], getattr(interface, f"set_{name}"))
    await fn(value)


def _on_signal(interface: ProxyInterface, name: str, callback: Callable[..., None]) -> None:
    fn = cast(Callable[[Callable[..., None]], None], getattr(interface, f"on_{name}"))
    fn(callback)


def _off_signal(interface: ProxyInterface, name: str, callback: Callable[..., None]) -> None:
    fn = cast(Callable[[Callable[..., None]], None], getattr(interface, f"off_{name}"))
    fn(callback)


class BluezClient:
    """Async context manager wrapping the ``org.bluez`` D-Bus API.

    Usage::

        async with BluezClient() as bluez:
            async with bluez.pairing_agent():
                mac = await bluez.discover_bredr_address(timeout=60.0)
                if mac is not None:
                    await bluez.pair(mac)
                    await bluez.trust(mac)
                    await bluez.connect(mac)
    """

    def __init__(self) -> None:
        self._bus: MessageBus | None = None

    async def __aenter__(self) -> "BluezClient":
        self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None

    @property
    def _connected_bus(self) -> MessageBus:
        if self._bus is None:
            raise RuntimeError("BluezClient used outside 'async with' block")
        return self._bus

    async def _proxy(self, path: str, bus_name: str = _BLUEZ) -> ProxyObject:
        bus = self._connected_bus
        introspection = await bus.introspect(bus_name, path)
        return bus.get_proxy_object(bus_name, path, introspection)

    async def _adapter(self) -> ProxyInterface:
        obj = await self._proxy(_ADAPTER_PATH)
        return obj.get_interface("org.bluez.Adapter1")

    async def _agent_manager(self) -> ProxyInterface:
        obj = await self._proxy("/org/bluez")
        return obj.get_interface("org.bluez.AgentManager1")

    async def _device(self, mac: str) -> ProxyInterface:
        obj = await self._proxy(_device_path(mac))
        return obj.get_interface("org.bluez.Device1")

    # ------------------------------------------------------------------
    # Bondable mode — scoped to the pairing operation (ADR-027 decision 2)
    # ------------------------------------------------------------------

    async def set_pairable(self, enabled: bool) -> None:
        adapter = await self._adapter()
        await _set_property(adapter, "pairable", enabled)

    # ------------------------------------------------------------------
    # Agent — required before Pair() will proceed at all
    # ------------------------------------------------------------------

    def pairing_agent(self) -> "_AgentScope":
        return _AgentScope(self)

    async def _register_agent(self) -> None:
        bus = self._connected_bus
        bus.export(_AGENT_PATH, _PairingAgent())
        manager = await self._agent_manager()
        await _call(manager, "register_agent", _AGENT_PATH, _AGENT_CAPABILITY)
        await _call(manager, "request_default_agent", _AGENT_PATH)

    async def _unregister_agent(self) -> None:
        try:
            manager = await self._agent_manager()
            await _call(manager, "unregister_agent", _AGENT_PATH)
        except DBusError as exc:
            log.debug("Pairing: agent unregister failed (already gone?): %s", exc)
        finally:
            self._connected_bus.unexport(_AGENT_PATH)

    # ------------------------------------------------------------------
    # Discovery — event-driven, pairs immediately on first match (ADR-027 decision 4)
    # ------------------------------------------------------------------

    async def discover_bredr_address(self, timeout: float) -> str | None:
        """Discover the JBL's BR/EDR address from its LE advertisement.

        Starts LE discovery and resolves as soon as any device's Harman
        FDDF service data yields a valid address — including a device
        already known to BlueZ from a previous session. Does not wait out
        the full timeout once a match is found.

        **First-match-wins.** If two JBL/Harman speakers are simultaneously
        in pairing mode within range, the appliance pairs with whichever
        FDDF advertisement arrives first. There is no disambiguation or
        user-confirmation step — consistent with the ``NoInputNoOutput``
        agent capability. This is intentional for v1.0 (one speaker, one
        user, one button press); multi-speaker selection is deferred.
        """
        found: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        property_subscriptions: list[tuple[ProxyInterface, Callable[..., None]]] = []
        watch_tasks: set[asyncio.Task[None]] = set()

        def _maybe_resolve(service_data: dict[str, Any]) -> None:
            if found.done():
                return
            raw = service_data.get(HARMAN_FDDF_UUID)
            if raw is None:
                return
            if isinstance(raw, Variant):
                raw = raw.value
            mac = extract_bredr_address(bytes(raw))
            if mac is not None:
                found.set_result(mac)

        async def _watch_existing_device(path: str) -> None:
            try:
                obj = await self._proxy(path)
            except DBusError:
                return
            props = obj.get_interface("org.freedesktop.DBus.Properties")

            def on_changed(
                _interface: str, changed: dict[str, Variant], _invalidated: list[str]
            ) -> None:
                if "ServiceData" in changed:
                    _maybe_resolve(changed["ServiceData"].value)

            _on_signal(props, "properties_changed", on_changed)
            property_subscriptions.append((props, on_changed))

            try:
                device = obj.get_interface("org.bluez.Device1")
                service_data = await _get_property(device, "service_data")
                if service_data:
                    _maybe_resolve(service_data)
            except DBusError:
                pass

        def on_interfaces_added(path: str, interfaces: dict[str, dict[str, Variant]]) -> None:
            if "org.bluez.Device1" not in interfaces:
                return
            service_data_variant = interfaces["org.bluez.Device1"].get("ServiceData")
            if service_data_variant is not None:
                _maybe_resolve(service_data_variant.value)
            task = asyncio.ensure_future(_watch_existing_device(path))
            watch_tasks.add(task)
            task.add_done_callback(watch_tasks.discard)

        obj_manager_obj = await self._proxy("/")
        obj_manager = obj_manager_obj.get_interface("org.freedesktop.DBus.ObjectManager")
        _on_signal(obj_manager, "interfaces_added", on_interfaces_added)

        try:
            existing = await _call(obj_manager, "get_managed_objects")
            for path, interfaces in existing.items():
                if "org.bluez.Device1" in interfaces:
                    await _watch_existing_device(path)
                    if found.done():
                        break

            adapter = await self._adapter()
            if not found.done():
                await _call(adapter, "set_discovery_filter", {"Transport": Variant("s", "le")})
                try:
                    await _call(adapter, "start_discovery")
                except DBusError as exc:
                    if "InProgress" not in exc.type:
                        raise
                    log.debug("Pairing: discovery already in progress, continuing")
                try:
                    return await asyncio.wait_for(found, timeout=timeout)
                except TimeoutError:
                    return None
            return found.result()
        finally:
            _off_signal(obj_manager, "interfaces_added", on_interfaces_added)
            for props, handler in property_subscriptions:
                _off_signal(props, "properties_changed", handler)
            for task in watch_tasks:
                task.cancel()
            if watch_tasks:
                await asyncio.gather(*watch_tasks, return_exceptions=True)
            try:
                adapter = await self._adapter()
                await _call(adapter, "stop_discovery")
            except DBusError:
                pass

    # ------------------------------------------------------------------
    # Pair / Trust / Connect
    # ------------------------------------------------------------------

    async def pair(self, mac: str) -> None:
        device = await self._device(mac)
        try:
            await asyncio.wait_for(_call(device, "pair"), timeout=_PAIR_TIMEOUT)
        except DBusError as exc:
            if "AlreadyExists" in exc.type:
                log.info("Pairing: %s was already paired", mac)
                return
            raise PairingFailedError(f"Pair failed for {mac}: {exc}") from exc
        except TimeoutError as exc:
            raise PairingFailedError(f"Pair timed out for {mac}") from exc

    async def trust(self, mac: str) -> None:
        device = await self._device(mac)
        await asyncio.wait_for(_set_property(device, "trusted", True), timeout=_TRUST_TIMEOUT)

    async def connect(self, mac: str) -> None:
        device = await self._device(mac)
        try:
            await asyncio.wait_for(_call(device, "connect"), timeout=_CONNECT_TIMEOUT)
        except (DBusError, TimeoutError) as exc:
            # Non-fatal: AudioService independently retries A2DP connection.
            log.warning("Pairing: connect failed for %s: %s", mac, exc)

    # ------------------------------------------------------------------
    # A2DP connection management (used by AudioService)
    # ------------------------------------------------------------------

    async def is_connected(self, mac: str) -> bool:
        """Read Device1.Connected — True when any profile is connected."""
        device = await self._device(mac)
        try:
            return bool(await asyncio.wait_for(_get_property(device, "connected"), timeout=5.0))
        except (DBusError, TimeoutError):
            return False

    async def connect_a2dp(self, mac: str) -> None:
        """Call Device1.ConnectProfile(A2DP_SINK_UUID) and raise on failure.

        Uses ConnectProfile rather than Connect so that only the A2DP Audio
        Sink profile is requested.  Calling Connect() triggers BlueZ to
        negotiate ALL profiles simultaneously, which causes a deadlock-like
        stall when WirePlumber — the A2DP endpoint owner — is busy handling
        other BlueZ callbacks on the same connection.  ConnectProfile is what
        WirePlumber itself uses for auto-connect and completes reliably.

        Raises :exc:`A2dpProfileUnavailableError` when the speaker rejects A2DP
        (the ACL link is already gone; don't call disconnect after this).

        Raises :exc:`A2dpConnectError` for any other failure (a stale ACL may
        still be up; callers should call disconnect_a2dp to clean up).
        """
        _A2DP_SINK_UUID = "0000110b-0000-1000-8000-00805f9b34fb"
        device = await self._device(mac)
        try:
            await asyncio.wait_for(
                _call(device, "connect_profile", _A2DP_SINK_UUID),
                timeout=_CONNECT_TIMEOUT,
            )
        except TimeoutError as exc:
            # The asyncio timeout fired, but BlueZ keeps negotiating in the
            # background (dbus-fast cancels the task, not the D-Bus call).
            # Check connectivity: if it's up, treat the slow success as good.
            if await self.is_connected(mac):
                return
            raise A2dpConnectError(f"connect timed out for {mac}") from exc
        except DBusError as exc:
            msg = str(exc)
            if (
                "br-connection-profile-unavailable" in msg
                or "br-connection-unknown" in msg
                or "NotAvailable" in msg
            ):
                raise A2dpProfileUnavailableError(msg) from exc
            if "AlreadyConnected" in msg or "br-connection-busy" in msg or "InProgress" in msg:
                return  # already connected or connecting — success
            raise A2dpConnectError(msg) from exc

    async def disconnect_a2dp(self, mac: str) -> None:
        """Call Device1.Disconnect(). No-op if already disconnected."""
        device = await self._device(mac)
        try:
            await asyncio.wait_for(_call(device, "disconnect"), timeout=5.0)
        except DBusError as exc:
            if "NotConnected" not in str(exc):
                log.debug("A2DP disconnect for %s: %s", mac, exc)


class _AgentScope:
    """Async context manager registering/unregistering the pairing agent."""

    def __init__(self, client: BluezClient) -> None:
        self._client = client

    async def __aenter__(self) -> None:
        await self._client._register_agent()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._client._unregister_agent()


def _device_path(mac: str) -> str:
    return f"{_ADAPTER_PATH}/dev_{mac.replace(':', '_').upper()}"
