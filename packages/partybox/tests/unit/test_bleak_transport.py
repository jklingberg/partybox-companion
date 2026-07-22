"""CI-safe unit tests for BleakTransport.

These cover construction, the error model, and the notification/disconnect
logic that can be exercised without a Bluetooth adapter (by driving the bleak
callbacks directly). Real connection behaviour is covered by the
hardware-marked tests in ``tests/integration/test_hardware.py``.
"""

import asyncio

import pytest
from bleak.exc import BleakDBusError, BleakError
from partybox.bluetooth import (
    CONTROL_SERVICE_UUID,
    RX_CHAR_UUID,
    TX_CHAR_UUID,
    BleakTransport,
    ConfirmedDisconnectError,
    ConnectionFailedError,
    ConnectionLostError,
    ControlTransport,
    NotConnectedError,
)
from partybox.bluetooth import bleak_transport as bleak_transport_module


def test_is_a_bluetooth_backend() -> None:
    assert isinstance(BleakTransport("AA:BB:CC:DD:EE:FF"), ControlTransport)


def test_address_property() -> None:
    assert BleakTransport("AA:BB:CC:DD:EE:FF").address == "AA:BB:CC:DD:EE:FF"


def test_not_connected_initially() -> None:
    assert not BleakTransport("AA:BB:CC:DD:EE:FF").is_connected


def test_excelpoint_control_uuids() -> None:
    # UUID base is ASCII "excelpoint.com"; service ends 0000, TX 0002, RX 0001.
    assert CONTROL_SERVICE_UUID == "65786365-6c70-6f69-6e74-2e636f6d0000"
    assert TX_CHAR_UUID == "65786365-6c70-6f69-6e74-2e636f6d0002"
    assert RX_CHAR_UUID == "65786365-6c70-6f69-6e74-2e636f6d0001"


async def test_write_before_connect_raises_not_connected() -> None:
    backend = BleakTransport("AA:BB:CC:DD:EE:FF")
    with pytest.raises(NotConnectedError):
        await backend.write(b"x")


async def test_receive_before_connect_raises_not_connected() -> None:
    backend = BleakTransport("AA:BB:CC:DD:EE:FF")
    with pytest.raises(NotConnectedError):
        await backend.receive()


async def test_disconnect_when_not_connected_is_noop() -> None:
    backend = BleakTransport("AA:BB:CC:DD:EE:FF")
    await backend.disconnect()  # must not raise
    assert not backend.is_connected


async def test_notification_callback_delivers_to_receive() -> None:
    # Simulate an established connection and drive the bleak notify callback.
    backend = BleakTransport("AA:BB:CC:DD:EE:FF")
    backend._client = object()  # type: ignore[assignment]
    backend._on_notify(None, bytearray(b"\xaa\x03\x01\x05"))  # type: ignore[arg-type]
    assert await backend.receive() == b"\xaa\x03\x01\x05"


async def test_unexpected_disconnect_callback_raises_connection_lost() -> None:
    backend = BleakTransport("AA:BB:CC:DD:EE:FF")
    backend._client = object()  # type: ignore[assignment]
    backend._on_disconnect(None)  # type: ignore[arg-type]  # speaker dropped
    assert not backend.is_connected
    with pytest.raises(ConnectionLostError):
        await backend.receive()


async def test_unexpected_disconnect_callback_raises_confirmed_disconnect() -> None:
    """More specific than the test above: bleak's own disconnected-callback
    firing is a *confirmed* disconnect (ConfirmedDisconnectError), not merely
    a failed attempt (the plain ConnectionLostError base class) — a caller
    tolerating transient ConnectionLostError failures (partyboxd's
    health-check retry) must be able to tell these apart."""
    backend = BleakTransport("AA:BB:CC:DD:EE:FF")
    backend._client = object()  # type: ignore[assignment]
    backend._on_disconnect(None)  # type: ignore[arg-type]
    with pytest.raises(ConfirmedDisconnectError):
        await backend.receive()


async def test_require_connected_raises_confirmed_disconnect_after_lost() -> None:
    backend = BleakTransport("AA:BB:CC:DD:EE:FF")
    backend._client = object()  # type: ignore[assignment]
    backend._on_disconnect(None)  # type: ignore[arg-type]
    with pytest.raises(ConfirmedDisconnectError):
        await backend.write(b"\xaa\x03\x01")


class _HungClient:
    """Client whose GATT calls never complete.

    Models a connection dying with a D-Bus call in flight: bluetoothd never
    sends the method reply, so the awaitable never resolves (the wedge behind
    the frozen device manager observed on hardware 2026-07-16).
    """

    async def write_gatt_char(self, *args: object, **kwargs: object) -> None:
        await asyncio.Event().wait()

    async def read_gatt_char(self, *args: object, **kwargs: object) -> bytes:
        await asyncio.Event().wait()
        return b""

    async def start_notify(self, *args: object, **kwargs: object) -> None:
        await asyncio.Event().wait()

    async def disconnect(self) -> None:
        await asyncio.Event().wait()


async def test_hung_write_raises_connection_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bleak_transport_module, "_GATT_IO_TIMEOUT", 0.05)
    backend = BleakTransport("AA:BB:CC:DD:EE:FF")
    backend._client = _HungClient()  # type: ignore[assignment]
    with pytest.raises(ConnectionLostError):
        await backend.write(b"\xaa\x03\x01")


class _RaisingClient:
    """Client whose write/read raise a specific exception immediately."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def write_gatt_char(self, *args: object, **kwargs: object) -> None:
        raise self._exc

    async def read_gatt_char(self, *args: object, **kwargs: object) -> bytes:
        raise self._exc


async def test_write_unknown_object_dbus_error_raises_confirmed_disconnect() -> None:
    """Live incident (2026-07-22): a write failed with exactly this D-Bus
    error after BlueZ had already torn down the characteristic object —
    confirmed gone, not a transient failure worth retrying."""
    backend = BleakTransport("AA:BB:CC:DD:EE:FF")
    backend._client = _RaisingClient(  # type: ignore[assignment]
        BleakDBusError("org.freedesktop.DBus.Error.UnknownObject", ["doesn't exist"])
    )
    with pytest.raises(ConfirmedDisconnectError):
        await backend.write(b"\xaa\x21\x00")


async def test_read_unknown_object_dbus_error_raises_confirmed_disconnect() -> None:
    backend = BleakTransport("AA:BB:CC:DD:EE:FF")
    backend._client = _RaisingClient(  # type: ignore[assignment]
        BleakDBusError("org.freedesktop.DBus.Error.UnknownObject", ["doesn't exist"])
    )
    with pytest.raises(ConfirmedDisconnectError):
        await backend.read("0000180f-0000-1000-8000-00805f9b34fb")


async def test_write_generic_bleak_error_raises_plain_connection_lost() -> None:
    """Not every write failure means the object is confirmed gone — an
    ordinary BleakError (e.g. a transient GATT protocol error) must stay a
    plain ConnectionLostError, tolerated by a caller's retry logic, not
    escalated to ConfirmedDisconnectError."""
    backend = BleakTransport("AA:BB:CC:DD:EE:FF")
    backend._client = _RaisingClient(BleakError("some transient GATT hiccup"))  # type: ignore[assignment]
    with pytest.raises(ConnectionLostError) as exc_info:
        await backend.write(b"\xaa\x21\x00")
    assert not isinstance(exc_info.value, ConfirmedDisconnectError)


async def test_write_other_confirmed_gone_dbus_errors_raise_confirmed_disconnect() -> None:
    backend = BleakTransport("AA:BB:CC:DD:EE:FF")
    for dbus_error in ("org.freedesktop.DBus.Error.ServiceUnknown", "org.bluez.Error.NotConnected"):
        backend._client = _RaisingClient(BleakDBusError(dbus_error, []))  # type: ignore[assignment]
        with pytest.raises(ConfirmedDisconnectError):
            await backend.write(b"\xaa\x21\x00")


async def test_hung_read_raises_connection_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bleak_transport_module, "_GATT_IO_TIMEOUT", 0.05)
    backend = BleakTransport("AA:BB:CC:DD:EE:FF")
    backend._client = _HungClient()  # type: ignore[assignment]
    with pytest.raises(ConnectionLostError):
        await backend.read("65786365-6c70-6f69-6e74-2e636f6d0001")


async def test_hung_disconnect_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """bleak's Disconnect D-Bus call can hang on a lost reply; disconnect()
    must complete anyway (it runs during daemon shutdown)."""
    monkeypatch.setattr(bleak_transport_module, "_GATT_IO_TIMEOUT", 0.05)
    backend = BleakTransport("AA:BB:CC:DD:EE:FF")
    backend._client = _HungClient()  # type: ignore[assignment]
    await asyncio.wait_for(backend.disconnect(), timeout=1.0)  # must not hang
    assert not backend.is_connected


async def test_connect_cleanup_is_bounded_when_start_notify_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung start_notify times out into ConnectionFailedError, and the
    cleanup disconnect (which also hangs here) must not re-freeze connect()."""
    monkeypatch.setattr(bleak_transport_module, "_GATT_IO_TIMEOUT", 0.05)

    class _WedgedClient(_HungClient):
        async def connect(self) -> None:
            return None

    monkeypatch.setattr(
        bleak_transport_module,
        "BleakClient",
        lambda *_args, **_kwargs: _WedgedClient(),
    )
    backend = BleakTransport("AA:BB:CC:DD:EE:FF")

    async def _fake_resolve() -> str:
        return "AA:BB:CC:DD:EE:FF"

    monkeypatch.setattr(backend, "_resolve_device", _fake_resolve)
    with pytest.raises(ConnectionFailedError):
        await asyncio.wait_for(backend.connect(), timeout=1.0)
    assert not backend.is_connected
