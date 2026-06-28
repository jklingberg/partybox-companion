"""Volume capability — get and set the speaker's hardware volume."""

from __future__ import annotations


class VolumeCapability:
    """Controls the speaker's hardware volume level (0-100 %).

    The BLE GATT opcode for hardware volume has not yet been confirmed from
    hardware captures. Both methods raise :exc:`NotImplementedError` until
    the opcode is identified and validated on real hardware.
    """

    async def get(self) -> int:
        """Return the current volume as a percentage (0-100).

        Raises:
            NotImplementedError: BLE volume opcode not yet confirmed.
        """
        raise NotImplementedError("BLE volume opcode not yet confirmed")

    async def set(self, percent: int) -> None:
        """Set the speaker volume to *percent* (0-100 inclusive).

        Args:
            percent: Target volume level, 0-100 inclusive.

        Raises:
            ValueError: if *percent* is outside [0, 100].
            NotImplementedError: BLE volume opcode not yet confirmed.
        """
        if not (0 <= percent <= 100):
            raise ValueError(f"percent must be 0-100, got {percent!r}")
        raise NotImplementedError("BLE volume opcode not yet confirmed")
