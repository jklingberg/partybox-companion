"""PartyBoxDevice — the main SDK entry point for a single connected speaker."""

from __future__ import annotations

from types import TracebackType

from partybox.bluetooth.scanner import PartyBoxCandidate
from partybox.bluetooth.transport import ControlTransport, NotConnectedError
from partybox.protocol.constants import BATTERY_SERVICE_UUID

from .capabilities.battery import BatteryCapability
from .capabilities.device_info import DeviceInfoCapability
from .capabilities.power import PowerCapability


class PartyBoxDevice:
    """A PartyBox speaker that can be connected to and controlled.

    Create via :class:`~partybox.Scanner`::

        speaker = await Scanner.find()
        await speaker.connect()
        await speaker.power.turn_on()
        print(await speaker.device_info.model())
        await speaker.disconnect()

    Or as an async context manager::

        async with await Scanner.find() as speaker:
            await speaker.power.turn_on()

    Args:
        candidate: a discovered speaker returned by
            :class:`~partybox.bluetooth.Scanner`. Callers normally receive
            this via the top-level :class:`~partybox.Scanner`, not directly.
    """

    def __init__(self, candidate: PartyBoxCandidate) -> None:
        self._candidate: PartyBoxCandidate | None = candidate
        self._transport: ControlTransport | None = None
        self._power: PowerCapability | None = None
        self._device_info: DeviceInfoCapability | None = None
        self._battery: BatteryCapability | None = None

    @classmethod
    def _from_transport(cls, transport: ControlTransport) -> PartyBoxDevice:
        """Build a device from an already-connected transport.

        For testing only — allows capabilities to be exercised with a
        :class:`~partybox.bluetooth.MockTransport` without needing a real BLE
        candidate or an actual ``connect()`` call.
        """
        obj: PartyBoxDevice = object.__new__(cls)
        obj._candidate = None
        obj._transport = transport
        obj._power = PowerCapability(transport)
        obj._device_info = DeviceInfoCapability(transport)
        has_battery = transport.has_service(BATTERY_SERVICE_UUID)
        obj._battery = BatteryCapability(transport) if has_battery else None
        return obj

    async def connect(self) -> None:
        """Connect to the speaker and initialise capabilities.

        Calling this on an already-connected device is a no-op.
        """
        if self._transport is not None:
            return
        if self._candidate is None:
            raise RuntimeError("device has no candidate (created via _from_transport)")
        transport: ControlTransport = await self._candidate.connect()
        self._power = PowerCapability(transport)
        self._device_info = DeviceInfoCapability(transport)
        if transport.has_service(BATTERY_SERVICE_UUID):
            self._battery = BatteryCapability(transport)
        self._transport = transport

    async def disconnect(self) -> None:
        """Disconnect from the speaker cleanly.

        Calling this when not connected is a no-op. After disconnecting,
        capability properties raise :class:`~partybox.NotConnectedError`
        until :meth:`connect` is called again.
        """
        if self._transport is not None:
            await self._transport.disconnect()
            self._transport = None
            self._power = None
            self._device_info = None
            self._battery = None

    @property
    def is_connected(self) -> bool:
        """Whether the control connection is currently live."""
        return self._transport is not None and self._transport.is_connected

    @property
    def power(self) -> PowerCapability:
        """Power the speaker on or off.

        Raises:
            NotConnectedError: if :meth:`connect` has not been called.
        """
        if self._power is None:
            raise NotConnectedError("call connect() before accessing capabilities")
        return self._power

    @property
    def device_info(self) -> DeviceInfoCapability:
        """Read static device attributes (model, manufacturer, firmware).

        Raises:
            NotConnectedError: if :meth:`connect` has not been called.
        """
        if self._device_info is None:
            raise NotConnectedError("call connect() before accessing capabilities")
        return self._device_info

    @property
    def battery(self) -> BatteryCapability | None:
        """Battery capability, or ``None`` on mains-powered models.

        The PartyBox 520 is mains-powered and returns ``None`` after connecting.
        Portable models (110, 310, …) expose the BLE Battery Service and return
        a :class:`~partybox.device.capabilities.BatteryCapability`.

        Raises:
            NotConnectedError: if :meth:`connect` has not been called.
        """
        if self._transport is None:
            raise NotConnectedError("call connect() before accessing capabilities")
        return self._battery

    async def __aenter__(self) -> PartyBoxDevice:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.disconnect()

    def __repr__(self) -> str:
        status = "connected" if self.is_connected else "disconnected"
        return f"PartyBoxDevice({status})"
