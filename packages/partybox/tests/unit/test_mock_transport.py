"""Unit tests for the MockTransport transport fake."""

import asyncio

import pytest
from partybox.bluetooth import (
    ConnectionFailedError,
    ConnectionLostError,
    ControlTransport,
    MockTransport,
    NotConnectedError,
)


def test_is_a_bluetooth_backend() -> None:
    assert isinstance(MockTransport(), ControlTransport)


def test_default_and_custom_address() -> None:
    assert MockTransport().address == "AA:BB:CC:DD:EE:FF"
    assert MockTransport("11:22:33:44:55:66").address == "11:22:33:44:55:66"


async def test_connect_disconnect_lifecycle() -> None:
    backend = MockTransport()
    assert not backend.is_connected

    await backend.connect()
    assert backend.is_connected

    await backend.disconnect()
    assert not backend.is_connected


async def test_connect_is_idempotent() -> None:
    backend = MockTransport()
    await backend.connect()
    await backend.connect()
    assert backend.is_connected


async def test_disconnect_when_not_connected_is_noop() -> None:
    backend = MockTransport()
    await backend.disconnect()  # must not raise
    assert not backend.is_connected


async def test_fail_on_connect() -> None:
    backend = MockTransport(fail_on_connect=True)
    with pytest.raises(ConnectionFailedError):
        await backend.connect()
    assert not backend.is_connected


async def test_async_context_manager_connects_and_disconnects() -> None:
    backend = MockTransport()
    async with backend as entered:
        assert entered is backend
        assert backend.is_connected
    assert not backend.is_connected


async def test_receive_returns_fed_notification() -> None:
    backend = MockTransport()
    await backend.connect()
    backend.feed(b"\xaa\x55\x01")
    assert await backend.receive() == b"\xaa\x55\x01"


async def test_notifications_are_whole_messages_in_order() -> None:
    backend = MockTransport()
    await backend.connect()
    backend.feed(b"first")
    backend.feed(b"second")
    assert await backend.receive() == b"first"
    assert await backend.receive() == b"second"


async def test_receive_blocks_until_notification_is_fed() -> None:
    backend = MockTransport()
    await backend.connect()

    async def feed_later() -> None:
        await asyncio.sleep(0.01)
        backend.feed(b"hi")

    task = asyncio.create_task(feed_later())
    assert await backend.receive() == b"hi"
    await task


async def test_write_records_commands() -> None:
    backend = MockTransport()
    await backend.connect()
    await backend.write(b"one")
    await backend.write(b"two")
    assert backend.writes == [b"one", b"two"]


async def test_stub_auto_responds_to_matching_command() -> None:
    backend = MockTransport()
    command = bytes.fromhex("aa030105")
    response = bytes.fromhex("aa0301050100")
    backend.stub(command, response)

    await backend.connect()
    await backend.write(command)
    assert await backend.receive() == response


async def test_stub_does_not_respond_to_other_commands() -> None:
    backend = MockTransport()
    backend.stub(b"known", b"reply")
    await backend.connect()
    await backend.write(b"other")

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(backend.receive(), timeout=0.02)


async def test_io_before_connect_raises_not_connected() -> None:
    backend = MockTransport()
    with pytest.raises(NotConnectedError):
        await backend.write(b"x")
    with pytest.raises(NotConnectedError):
        await backend.receive()


async def test_io_after_clean_disconnect_raises_not_connected() -> None:
    backend = MockTransport()
    await backend.connect()
    await backend.disconnect()
    with pytest.raises(NotConnectedError):
        await backend.receive()


async def test_drop_raises_connection_lost_on_subsequent_io() -> None:
    backend = MockTransport()
    await backend.connect()
    backend.drop()
    assert not backend.is_connected
    with pytest.raises(ConnectionLostError):
        await backend.write(b"x")
    with pytest.raises(ConnectionLostError):
        await backend.receive()


async def test_drop_wakes_a_blocked_receive() -> None:
    backend = MockTransport()
    await backend.connect()

    async def drop_later() -> None:
        await asyncio.sleep(0.01)
        backend.drop()

    task = asyncio.create_task(drop_later())
    with pytest.raises(ConnectionLostError):
        await backend.receive()
    await task


async def test_drop_then_reconnect_clears_lost_state() -> None:
    backend = MockTransport()
    await backend.connect()
    backend.drop()
    await backend.connect()
    assert backend.is_connected

    backend.feed(b"ok")
    assert await backend.receive() == b"ok"


async def test_disconnect_clears_buffered_notifications() -> None:
    backend = MockTransport()
    await backend.connect()
    backend.feed(b"stale")
    await backend.disconnect()
    await backend.connect()

    backend.feed(b"fresh")
    assert await backend.receive() == b"fresh"
