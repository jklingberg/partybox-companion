"""Power capability — turn the speaker on and off."""

from __future__ import annotations

from partybox.bluetooth.transport import ControlTransport
from partybox.protocol.codec import encode
from partybox.protocol.messages import PowerCommand, PowerState


class PowerCapability:
    """Controls the speaker's power state.

    Commands are fire-and-forget: the PartyBox does not send a notification
    response to power commands (confirmed during M3 hardware testing). The
    GATT write-with-response at the transport layer confirms delivery.
    """

    def __init__(self, transport: ControlTransport) -> None:
        self._transport = transport

    async def turn_on(self) -> None:
        """Power the speaker on."""
        await self._transport.write(encode(PowerCommand(PowerState.ON)))

    async def turn_off(self) -> None:
        """Power the speaker off."""
        await self._transport.write(encode(PowerCommand(PowerState.OFF)))
