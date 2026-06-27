"""Control transport abstraction for the PartyBox.

The PartyBox is controlled over BLE GATT: commands are written to a vendor
characteristic and the speaker replies via notifications on a companion
characteristic (see ADR-015). The transport is therefore *message-oriented* —
each notification is delivered whole, not as a byte stream. This module defines
the boundary between *transport* (moving message payloads) and *protocol*
(interpreting them, handled in ``partybox.protocol``).

This is the **control** channel specifically. Bluetooth Classic A2DP (audio) is
a separate transport handled outside the SDK; naming this ``ControlTransport``
keeps the two unambiguous as the audio path is introduced (M3).

Callers depend only on :class:`ControlTransport`. The concrete transports
(``BleakTransport``, ``MockTransport``) are never imported outside this package
and the test fixtures.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from types import TracebackType


class BluetoothError(Exception):
    """Base class for all transport-layer errors."""


class NotConnectedError(BluetoothError):
    """Raised when an operation requires an active connection but none exists.

    This indicates the transport was never connected, or was disconnected
    cleanly via :meth:`ControlTransport.disconnect`.
    """


class ConnectionLostError(BluetoothError):
    """Raised when an established connection drops unexpectedly.

    Distinct from :class:`NotConnectedError`: the connection *was* live and was
    lost mid-session (the speaker powered off, went out of range, etc.). The
    device layer uses this signal to trigger reconnection.
    """


class ConnectionFailedError(BluetoothError):
    """Raised when an attempt to establish a connection fails."""


class ControlTransport(ABC):
    """Abstract control transport to a single speaker.

    A transport owns one control connection. It knows nothing about the
    protocol carried over it: it sends opaque command payloads and surfaces
    opaque notification payloads.

    The transport is message-oriented. :meth:`write` sends one command payload
    to the speaker's control characteristic; :meth:`receive` returns the next
    notification payload, whole. Interpreting those payloads is the
    responsibility of ``partybox.protocol``.

    Transports are async context managers; entering connects and exiting
    disconnects::

        async with transport:
            await transport.write(command)
            response = await transport.receive()
    """

    @property
    @abstractmethod
    def address(self) -> str:
        """The Bluetooth address of the target device (``AA:BB:CC:DD:EE:FF``)."""

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Whether the transport currently has a live connection."""

    @abstractmethod
    async def connect(self) -> None:
        """Establish the connection.

        Calling this on an already-connected transport is a no-op.

        Raises:
            ConnectionFailedError: if the connection could not be established.
        """

    @abstractmethod
    async def disconnect(self) -> None:
        """Close the connection cleanly.

        Calling this on a transport that is not connected is a no-op. After a
        clean disconnect, subsequent I/O raises :class:`NotConnectedError`.
        """

    @abstractmethod
    async def write(self, data: bytes) -> None:
        """Send one command payload to the control characteristic.

        Raises:
            NotConnectedError: if the transport is not connected.
            ConnectionLostError: if the connection drops while writing.
        """

    @abstractmethod
    async def receive(self) -> bytes:
        """Return the next notification payload from the speaker.

        Blocks until a notification arrives and returns its full payload. A
        single logical consumer is assumed (the device layer's event loop).

        Raises:
            NotConnectedError: if the transport is not connected.
            ConnectionLostError: if the connection drops while waiting.
        """

    @abstractmethod
    async def read(self, uuid: str) -> bytes:
        """Read a GATT characteristic by UUID and return its value.

        Used for standard BLE profiles (Battery Service, Device Information)
        where the value is fetched with a direct ATT read rather than a
        write-then-notify exchange on the vendor control channel.

        Raises:
            NotConnectedError: if the transport is not connected.
            ConnectionLostError: if the connection drops during the read.
        """

    @abstractmethod
    def has_service(self, uuid: str) -> bool:
        """Whether the connected device exposes the given GATT service.

        Used at connect time to detect optional capabilities (e.g. Battery
        Service on portable models). Always returns ``False`` before the
        transport is connected.

        Args:
            uuid: lowercase 128-bit UUID string, e.g.
                ``"0000180f-0000-1000-8000-00805f9b34fb"``.
        """

    async def __aenter__(self) -> ControlTransport:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.disconnect()
