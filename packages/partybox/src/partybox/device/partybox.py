"""PartyBoxDevice — the main SDK entry point for a single connected speaker."""

from __future__ import annotations

from types import TracebackType

from partybox.bluetooth.scanner import PartyBoxCandidate
from partybox.bluetooth.transport import ControlTransport, NotConnectedError
from partybox.protocol.codec import encode
from partybox.protocol.messages import FirmwareVersionRequest

from .capabilities.battery import BatteryCapability
from .capabilities.device_info import DeviceInfoCapability
from .capabilities.power import PowerCapability
from .capabilities.volume import VolumeCapability

# Benign probe used by verify_connection(): the firmware-version request is
# harmless to the speaker (it may answer or ignore it; any reply is consumed
# by whatever drains notifications). What matters is that the ATT write
# round-trips the link.
_VERIFY_PROBE = encode(FirmwareVersionRequest())


# How long connect() waits for a battery reply before concluding the speaker
# has no battery. Generous vs. the ~1 s a real PartyBox takes, to avoid a false
# negative on a slow link.
_BATTERY_DETECT_TIMEOUT = 3.0


async def _detect_battery(
    transport: ControlTransport, *, timeout: float = _BATTERY_DETECT_TIMEOUT
) -> BatteryCapability | None:
    """Return a battery capability if the speaker answers a battery query.

    PartyBox models with a battery (including the 520) respond to the vendor
    battery request (opcode ``0x9D`` → ``0x9E``); they do not expose the
    standard BLE Battery Service, so a service probe cannot detect them. We
    detect by asking: a speaker that answers has a battery. A speaker that stays
    silent (times out) does not, and we return ``None``. Transport failures are
    left to propagate so the caller can treat them as a connection problem.
    """
    capability = BatteryCapability(transport)
    try:
        await capability.status(timeout=timeout)
    except TimeoutError:
        return None
    return capability


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
        self._volume: VolumeCapability | None = None

    @classmethod
    def _from_transport(
        cls, transport: ControlTransport, *, battery: bool = False
    ) -> PartyBoxDevice:
        """Build a device from an already-connected transport.

        For testing only — allows capabilities to be exercised with a
        :class:`~partybox.bluetooth.MockTransport` without needing a real BLE
        candidate or an actual ``connect()`` call. ``battery`` explicitly sets
        whether the battery capability is present (the real ``connect()`` path
        detects this by probing; see :func:`_detect_battery`).
        """
        obj: PartyBoxDevice = object.__new__(cls)
        obj._candidate = None
        obj._transport = transport
        obj._power = PowerCapability(transport)
        obj._device_info = DeviceInfoCapability(transport)
        obj._battery = BatteryCapability(transport) if battery else None
        obj._volume = VolumeCapability()
        return obj

    async def connect(self) -> None:
        """Connect to the speaker and initialise capabilities.

        Calling this on an already-connected device is a no-op. Calling it
        after a :class:`~partybox.ConnectionLostError` clears the dead
        transport and re-establishes the connection.
        """
        if self._transport is not None and self._transport.is_connected:
            return
        if self._transport is not None:
            # Dead transport from a prior ConnectionLostError — clean up before
            # reconnecting so capabilities are not left pointing at a dead object.
            await self._transport.disconnect()
            self._transport = None
            self._power = None
            self._device_info = None
            self._battery = None
            self._volume = None
        if self._candidate is None:
            raise RuntimeError("device has no candidate (created via _from_transport)")
        transport: ControlTransport = await self._candidate.connect()
        self._power = PowerCapability(transport)
        self._device_info = DeviceInfoCapability(transport)
        self._battery = await _detect_battery(transport)
        self._volume = VolumeCapability()
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
            self._volume = None

    @property
    def is_connected(self) -> bool:
        """Whether the control connection is currently live."""
        return self._transport is not None and self._transport.is_connected

    @property
    def address(self) -> str | None:
        """BLE address of the connected speaker, or ``None`` when not connected.

        The address is informational — PartyBox speakers rotate their private
        address, so this value should not be persisted or used to reconnect.
        """
        if self._transport is None:
            return None
        return self._transport.address

    async def verify_connection(self) -> None:
        """Prove the control link is alive with a real ATT round-trip.

        ``is_connected`` reads the transport's *cached* connection state,
        which can go stale when the disconnect callback never fires (for
        example after a bluetoothd restart, which exits without emitting
        the D-Bus signals the callback relies on). This method performs an
        actual write-with-response on the control characteristic, so a dead
        link raises instead of silently passing.

        Any reply the speaker sends is delivered to the notification queue
        and consumed by whoever is draining it; this method does not read.

        Raises:
            NotConnectedError: if not connected.
            ConnectionLostError: if the link is dead.
        """
        if self._transport is None:
            raise NotConnectedError("call connect() before verifying")
        await self._transport.write(_VERIFY_PROBE)

    async def drain_until_disconnect(self) -> None:
        """Drain unsolicited notifications until the connection drops.

        Blocks indefinitely, consuming and discarding every notification the
        speaker sends (button presses, state updates, etc.) until the control
        connection is lost. The name reflects the side effect: all messages
        arriving while this is running are silently consumed.

        Intended for the daemon's connection-maintenance loop, which does not
        process unsolicited notifications in M6. Capabilities that read from
        the receive queue (e.g. :meth:`~partybox.DeviceInfoCapability.firmware_version`)
        must complete before this is called, or they will race for messages.

        Raises:
            NotConnectedError: if not connected, or if the connection was
                closed cleanly via :meth:`disconnect`.
            ConnectionLostError: when an unexpected disconnect is detected
                (speaker powered off, went out of range, etc.).
        """
        if self._transport is None:
            raise NotConnectedError("call connect() before draining")
        # TODO: unsolicited notifications are intentionally discarded here until
        # they become first-class SDK events (a typed async iterable on Device).
        # When that happens, this loop should dispatch instead of discarding.
        while True:
            await self._transport.receive()

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
        """Battery capability, or ``None`` if the speaker has no battery.

        Detected at connect time by probing the vendor battery command: a
        speaker that answers (the PartyBox 520 and other battery models do)
        exposes a :class:`~partybox.device.capabilities.BatteryCapability`; one
        that stays silent returns ``None``. See :func:`_detect_battery`.

        Raises:
            NotConnectedError: if :meth:`connect` has not been called.
        """
        if self._transport is None:
            raise NotConnectedError("call connect() before accessing capabilities")
        return self._battery

    @property
    def volume(self) -> VolumeCapability:
        """Hardware volume control (0-100 %).

        Raises:
            NotConnectedError: if :meth:`connect` has not been called.
        """
        if self._volume is None:
            raise NotConnectedError("call connect() before accessing capabilities")
        return self._volume

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
