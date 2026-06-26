"""In-process fake transport for testing without hardware.

``MockTransport`` is the cornerstone of the project's test strategy: it lets the
protocol and device layers be exercised end-to-end in CI, with no real speaker.
It implements the full :class:`~partybox.bluetooth.transport.ControlTransport`
contract and adds test-only hooks to drive and inspect the conversation:

* :meth:`feed` queues a notification payload for the next :meth:`receive`.
* :meth:`stub` registers an automatic reply: when the host writes a given
  command, the matching notification is queued as if the speaker answered.
* :meth:`drop` simulates an unexpected disconnect mid-session.
* :attr:`writes` records every command written, for assertions.

Example::

    transport = MockTransport()
    transport.stub(POWER_ON_COMMAND, POWER_ON_NOTIFICATION)
    async with transport:
        await transport.write(POWER_ON_COMMAND)
        assert await transport.receive() == POWER_ON_NOTIFICATION
"""

from __future__ import annotations

import asyncio

from .transport import (
    ConnectionFailedError,
    ConnectionLostError,
    ControlTransport,
    NotConnectedError,
)

DEFAULT_ADDRESS = "AA:BB:CC:DD:EE:FF"


class MockTransport(ControlTransport):
    """A configurable, in-process fake :class:`ControlTransport`.

    Args:
        address: address this fake reports via :attr:`address`.
        fail_on_connect: if true, :meth:`connect` raises
            :class:`ConnectionFailedError` — for testing connection-failure
            handling.
    """

    def __init__(
        self,
        address: str = DEFAULT_ADDRESS,
        *,
        fail_on_connect: bool = False,
    ) -> None:
        self._address = address
        self.fail_on_connect = fail_on_connect

        self._connected = False
        # True once a live connection was lost unexpectedly (vs. never/cleanly
        # connected). Drives ConnectionLostError vs. NotConnectedError.
        self._lost = False

        # Queued notification payloads. None is a disconnect sentinel that
        # unblocks a waiting receive() so it can raise.
        self._inbox: asyncio.Queue[bytes | None] = asyncio.Queue()

        self._stubs: dict[bytes, bytes] = {}
        #: Every payload passed to :meth:`write`, in order.
        self.writes: list[bytes] = []

    # -- ControlTransport ---------------------------------------------------

    @property
    def address(self) -> str:
        return self._address

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        if self._connected:
            return
        if self.fail_on_connect:
            raise ConnectionFailedError(f"mock: refusing to connect to {self._address}")
        self._connected = True
        self._lost = False
        self._inbox = asyncio.Queue()

    async def disconnect(self) -> None:
        was_connected = self._connected
        self._connected = False
        self._lost = False
        if was_connected:
            # Wake any blocked receiver so it observes the clean disconnect.
            self._inbox.put_nowait(None)

    async def write(self, data: bytes) -> None:
        self._check_alive()
        self.writes.append(bytes(data))
        response = self._stubs.get(bytes(data))
        if response is not None:
            self.feed(response)

    async def receive(self) -> bytes:
        self._check_alive()
        item = await self._inbox.get()
        if item is None:
            # Sentinel queued by drop()/disconnect().
            self._check_alive()
            raise NotConnectedError("mock: disconnected")
        return item

    # -- Test hooks ---------------------------------------------------------

    def feed(self, data: bytes) -> None:
        """Queue ``data`` as a notification payload for :meth:`receive`."""
        self._inbox.put_nowait(bytes(data))

    def stub(self, command: bytes, response: bytes) -> None:
        """Register an automatic reply.

        When exactly ``command`` is written, ``response`` is queued as a
        notification, as though the speaker answered. Re-registering the same
        command replaces it.
        """
        self._stubs[bytes(command)] = bytes(response)

    def drop(self) -> None:
        """Simulate an unexpected connection drop.

        The connection is marked lost; pending and subsequent I/O raise
        :class:`ConnectionLostError` until :meth:`connect` is called again.
        """
        was_connected = self._connected
        self._connected = False
        self._lost = True
        if was_connected:
            # Wake any blocked receiver so it raises ConnectionLostError.
            self._inbox.put_nowait(None)

    # -- internals ----------------------------------------------------------

    def _check_alive(self) -> None:
        if self._lost:
            raise ConnectionLostError(f"mock: connection to {self._address} dropped")
        if not self._connected:
            raise NotConnectedError("mock: not connected")
