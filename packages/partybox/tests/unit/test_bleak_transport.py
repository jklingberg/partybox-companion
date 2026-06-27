"""CI-safe unit tests for BleakTransport.

These cover construction, the error model, and the notification/disconnect
logic that can be exercised without a Bluetooth adapter (by driving the bleak
callbacks directly). Real connection behaviour is covered by the
hardware-marked tests in ``tests/integration/test_hardware.py``.
"""

import pytest
from partybox.bluetooth import (
    CONTROL_SERVICE_UUID,
    RX_CHAR_UUID,
    TX_CHAR_UUID,
    BleakTransport,
    ConnectionLostError,
    ControlTransport,
    NotConnectedError,
)


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
