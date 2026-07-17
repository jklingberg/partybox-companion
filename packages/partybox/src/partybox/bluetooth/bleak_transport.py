"""BLE GATT control transport via ``bleak``.

Connects to a PartyBox over BLE and exposes its vendor control service: writes
command payloads to the TX characteristic and surfaces RX-characteristic
notifications through :meth:`receive`. ``bleak`` abstracts the platform backend
(BlueZ on Linux, CoreBluetooth on macOS), so this works on the Pi and on a dev
machine. See ADR-015 for why the SDK uses ``bleak``.

``bleak`` is an implementation detail: nothing in the public surface returns or
accepts a ``bleak`` type. Callers either pass an address string (typically a
bonded identity address) or — preferably — let :class:`~.scanner.Scanner`
discover a speaker and call ``candidate.connect()``, which binds this transport
to the live device handle and sidesteps the speaker's rotating private address.
"""

from __future__ import annotations

import asyncio
from typing import Final

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from .transport import (
    ConnectionFailedError,
    ConnectionLostError,
    ControlTransport,
    NotConnectedError,
)

#: Vendor control service (UUID base is ASCII "excelpoint.com").
CONTROL_SERVICE_UUID: Final = "65786365-6c70-6f69-6e74-2e636f6d0000"
#: Characteristic the host writes command frames to.
TX_CHAR_UUID: Final = "65786365-6c70-6f69-6e74-2e636f6d0002"
#: Characteristic the speaker sends notification responses on.
RX_CHAR_UUID: Final = "65786365-6c70-6f69-6e74-2e636f6d0001"

DEFAULT_CONNECT_TIMEOUT = 20.0

# bleak surfaces transport failures as BleakError; the underlying D-Bus/socket
# layer can also raise these. Treat them all as connection trouble.
_TRANSPORT_ERRORS = (BleakError, OSError, TimeoutError, EOFError)

# Ceiling on a single GATT operation (write/read/subscribe). BlueZ answers a
# write-with-response within a couple of seconds on a live link; a call that
# outlives this has lost its D-Bus reply — observed when the connection dies
# while the call is in flight and the device object vanishes: bluetoothd never
# replies and dbus-fast's MessageBus.call waits forever, freezing the caller
# on a connection that no longer exists. Bounding the call turns that hang
# into ConnectionLostError so callers can reconnect.
_GATT_IO_TIMEOUT = 10.0


class BleakTransport(ControlTransport):
    """BLE GATT control transport over ``bleak``.

    Args:
        address: target speaker BLE address. Prefer a bonded identity address;
            an unbonded address may be stale by connect time because the
            speaker rotates its private address (see ADR-015). When discovering
            via :class:`~.scanner.Scanner`, use ``candidate.connect()`` instead,
            which binds to the live device and avoids that race.
        tx_uuid: control characteristic to write commands to.
        rx_uuid: characteristic to subscribe to for notifications.
        connect_timeout: seconds to wait for :meth:`connect` before failing.
    """

    def __init__(
        self,
        address: str,
        *,
        tx_uuid: str = TX_CHAR_UUID,
        rx_uuid: str = RX_CHAR_UUID,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    ) -> None:
        self._address = address
        self._tx_uuid = tx_uuid
        self._rx_uuid = rx_uuid
        self._connect_timeout = connect_timeout

        # What connect() binds to: an address string (resolved fresh at connect
        # time) or a live BLEDevice captured during a scan (used directly).
        self._target: str | BLEDevice = address

        self._client: BleakClient | None = None
        self._inbox: asyncio.Queue[bytes | None] = asyncio.Queue()
        # Set by the disconnected callback for an *unexpected* drop; cleared on
        # connect. Distinguishes ConnectionLostError from NotConnectedError.
        self._lost = False
        # Set while we are intentionally tearing the connection down, so the
        # disconnected callback does not misread it as a drop.
        self._closing = False

    @classmethod
    def _for_device(
        cls,
        device: BLEDevice,
        *,
        tx_uuid: str = TX_CHAR_UUID,
        rx_uuid: str = RX_CHAR_UUID,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    ) -> BleakTransport:
        """Build a transport bound to an already-discovered live device.

        Internal — used by :class:`~.scanner.Scanner`. Connecting to the live
        ``BLEDevice`` avoids re-resolving a rotating private address.
        """
        transport = cls(
            device.address,
            tx_uuid=tx_uuid,
            rx_uuid=rx_uuid,
            connect_timeout=connect_timeout,
        )
        transport._target = device
        return transport

    @property
    def address(self) -> str:
        return self._address

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def connect(self) -> None:
        if self.is_connected:
            return
        self._lost = False
        self._closing = False
        self._inbox = asyncio.Queue()

        device = await self._resolve_device()
        client = BleakClient(
            device,
            disconnected_callback=self._on_disconnect,
            timeout=self._connect_timeout,
        )
        try:
            await client.connect()
            # start_notify is a GATT operation (CCCD write) — same lost-reply
            # hang risk as write/read, so it gets the same ceiling.
            await asyncio.wait_for(
                client.start_notify(self._rx_uuid, self._on_notify),
                timeout=_GATT_IO_TIMEOUT,
            )
        except _TRANSPORT_ERRORS as exc:
            with _suppress_transport_errors():
                # bleak's Disconnect D-Bus call is itself unbounded (only the
                # post-call event wait has an internal timeout), so an unbounded
                # await here could re-freeze the caller on the very failure this
                # cleanup handles. The wait_for TimeoutError lands in
                # _suppress_transport_errors.
                await asyncio.wait_for(client.disconnect(), timeout=_GATT_IO_TIMEOUT)
            raise ConnectionFailedError(f"could not connect to {self._address}: {exc}") from exc

        self._client = client
        # BlueZ fires spurious _on_disconnect callbacks for stale device-cache
        # entries while resolving a rotating private address to the identity
        # address during connect(). Those callbacks queue None sentinels in
        # _inbox and set _lost=True even though the connection we just made
        # is live. Drain the sentinels now (preserving any real notifications
        # that arrived after start_notify) and clear the lost flag.
        _drain_inbox_sentinels(self._inbox)
        self._lost = False

    async def disconnect(self) -> None:
        client, self._client = self._client, None
        self._closing = True
        try:
            if client is not None:
                with _suppress_transport_errors():
                    # Bounded for the same reason as in connect()'s cleanup:
                    # bleak's Disconnect D-Bus call can hang on a lost reply,
                    # and this path runs during daemon shutdown — an unbounded
                    # await here would stall shutdown until systemd's SIGKILL.
                    await asyncio.wait_for(client.disconnect(), timeout=_GATT_IO_TIMEOUT)
        finally:
            self._closing = False
            self._lost = False
            # Unblock any waiting receive() so it observes the disconnect.
            self._inbox.put_nowait(None)

    async def write(self, data: bytes) -> None:
        client = self._require_connected()
        try:
            await asyncio.wait_for(
                client.write_gatt_char(self._tx_uuid, data, response=True),
                timeout=_GATT_IO_TIMEOUT,
            )
        except _TRANSPORT_ERRORS as exc:
            raise ConnectionLostError(f"write to {self._address} failed: {exc}") from exc

    async def read(self, uuid: str) -> bytes:
        client = self._require_connected()
        try:
            return bytes(
                await asyncio.wait_for(client.read_gatt_char(uuid), timeout=_GATT_IO_TIMEOUT)
            )
        except _TRANSPORT_ERRORS as exc:
            raise ConnectionLostError(f"read from {self._address} failed: {exc}") from exc

    def has_service(self, uuid: str) -> bool:
        if self._client is None:
            return False
        needle = uuid.lower()
        for service in self._client.services:
            if service.uuid.lower() == needle:
                return True
        return False

    async def receive(self) -> bytes:
        self._require_connected()
        item = await self._inbox.get()
        if item is None:
            # Sentinel from disconnect()/_on_disconnect.
            if self._lost:
                raise ConnectionLostError(f"connection to {self._address} lost")
            raise NotConnectedError(f"disconnected from {self._address}")
        return item

    # -- internals ----------------------------------------------------------

    async def _resolve_device(self) -> str | BLEDevice:
        """Return what to hand BleakClient: a live device, or a fresh address.

        A live device (from discovery) is used as-is. An address string is
        re-scanned just before connecting, because the speaker rotates its
        private address and a stale string drops the link immediately.
        """
        if isinstance(self._target, BLEDevice):
            return self._target
        try:
            found = await BleakScanner.find_device_by_address(
                self._target, timeout=self._connect_timeout
            )
        except _TRANSPORT_ERRORS as exc:
            raise ConnectionFailedError(f"scan for {self._address} failed: {exc}") from exc
        if found is None:
            raise ConnectionFailedError(f"device {self._address} not found while scanning")
        return found

    def _on_notify(self, _characteristic: BleakGATTCharacteristic, data: bytearray) -> None:
        self._inbox.put_nowait(bytes(data))

    def _on_disconnect(self, _client: BleakClient) -> None:
        self._client = None
        if not self._closing:
            self._lost = True
        self._inbox.put_nowait(None)

    def _require_connected(self) -> BleakClient:
        if self._lost:
            raise ConnectionLostError(f"connection to {self._address} lost")
        if self._client is None:
            raise NotConnectedError(f"not connected to {self._address}")
        return self._client


def _drain_inbox_sentinels(inbox: asyncio.Queue[bytes | None]) -> None:
    """Remove None sentinels from inbox without discarding real notification bytes.

    Called after a successful connect() to flush the spurious None sentinels
    that BlueZ's disconnect callbacks queued while resolving a rotating private
    address. Any real notification bytes that arrived after start_notify are
    re-queued so they are not lost.
    """
    real: list[bytes] = []
    while not inbox.empty():
        try:
            item = inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if item is not None:
            real.append(item)
    for item in real:
        inbox.put_nowait(item)


class _suppress_transport_errors:
    """Context manager that swallows transport errors during teardown."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> bool:
        return exc_type is not None and issubclass(exc_type, _TRANSPORT_ERRORS)
