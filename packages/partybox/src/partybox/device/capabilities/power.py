"""Power capability — turn the speaker on and off."""

from __future__ import annotations

from partybox.bluetooth.transport import ControlTransport
from partybox.protocol.codec import encode
from partybox.protocol.messages import PowerCommand, PowerState


class PowerCapability:
    """Controls the speaker's power state.

    Commands are fire-and-forget at the SDK level: the speaker does send an
    ACK notification and a state update on the RX characteristic, but the SDK
    does not wait for or consume them. The GATT write-with-response at the
    transport layer confirms delivery.
    """

    def __init__(self, transport: ControlTransport) -> None:
        self._transport = transport

    async def turn_on(self) -> None:
        """Power the speaker on."""
        await self._transport.write(encode(PowerCommand(PowerState.ON)))

    async def turn_off(self) -> None:
        """Power the speaker off."""
        await self._transport.write(encode(PowerCommand(PowerState.OFF)))
