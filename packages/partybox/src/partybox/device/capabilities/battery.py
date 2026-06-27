"""Battery capability — available on portable PartyBox models only."""

from __future__ import annotations

from partybox.bluetooth.transport import ControlTransport
from partybox.protocol.constants import BATTERY_LEVEL_CHAR_UUID


class BatteryCapability:
    """Reads battery level from the standard BLE Battery Service (``0x180F``).

    Present only on models with an internal battery (e.g. PartyBox 110, 310).
    The PartyBox 520 is mains-powered and does not expose this service; callers
    should check ``speaker.battery is not None`` before using.
    """

    def __init__(self, transport: ControlTransport) -> None:
        self._transport = transport

    async def level(self) -> int:
        """Return the battery charge level as a percentage (0-100)."""
        data = await self._transport.read(BATTERY_LEVEL_CHAR_UUID)
        return data[0]
