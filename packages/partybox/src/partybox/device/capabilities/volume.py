"""Volume capability — get and set the speaker's output level.

The BLE GATT opcode for volume is not yet confirmed from hardware captures.
Both :meth:`get` and :meth:`set` raise :exc:`NotImplementedError` until the
opcode is identified and a real implementation is added.
"""

from __future__ import annotations

from partybox.bluetooth.transport import ControlTransport


class VolumeCapability:
    """Controls the speaker's output volume (0-100 percent).

    This capability is always present on every :class:`~partybox.PartyBoxDevice`
    after connecting, unlike :class:`~partybox.device.capabilities.BatteryCapability`
    which is only available on battery-powered models.

    .. note::
        The BLE GATT opcode for volume has not yet been confirmed from hardware
        captures. Both :meth:`get` and :meth:`set` raise :exc:`NotImplementedError`
        until the opcode is identified and validated.

    Args:
        transport: the active control transport for the connected speaker.
    """

    def __init__(self, transport: ControlTransport) -> None:
        self._transport = transport

    async def get(self) -> int:
        """Return the current volume level as a percentage (0-100).

        Raises:
            NotImplementedError: BLE opcode is not yet confirmed from hardware
                captures. This method will be implemented once the opcode is
                known.
        """
        raise NotImplementedError("volume BLE opcode not yet confirmed from hardware captures")

    async def set(self, percent: int) -> None:
        """Set the speaker volume to *percent* (0-100).

        Args:
            percent: the desired volume level, 0 (silent) to 100 (maximum).

        Raises:
            ValueError: if *percent* is outside the range 0-100.
            NotImplementedError: BLE opcode is not yet confirmed from hardware
                captures. This method will be implemented once the opcode is
                known.
        """
        if not 0 <= percent <= 100:
            raise ValueError(f"volume percent must be 0-100, got {percent!r}")
        raise NotImplementedError("volume BLE opcode not yet confirmed from hardware captures")
