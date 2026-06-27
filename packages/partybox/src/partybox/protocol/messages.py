"""Typed message dataclasses for the PartyBox vendor protocol.

Each message type maps to one logical command or notification. The codec
(``partybox.protocol.codec``) converts between message objects and raw bytes.

Only messages whose bytes are confirmed from hardware captures are defined here.
See docs/reverse-engineering/discoveries.md for the evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class PowerState(IntEnum):
    """Power state payload values, confirmed from hardware captures."""

    ON = 0x05  # AA 03 01 05
    OFF = 0x04  # AA 03 01 04


@dataclass(frozen=True)
class PowerCommand:
    """Command to set the speaker's power state.

    Args:
        state: :attr:`PowerState.ON` or :attr:`PowerState.OFF`.
    """

    state: PowerState


@dataclass(frozen=True)
class FirmwareVersionRequest:
    """Request the speaker's firmware version.

    Encodes to ``AA 21 00``. The speaker responds with a
    :class:`FirmwareVersionResponse` notification.
    """


@dataclass(frozen=True)
class FirmwareVersionResponse:
    """Firmware version notification from the speaker.

    Decoded from ``AA 22 04 [major] [minor] [patch] [0x00]``.
    Confirmed from a JBL PartyBox 520 running firmware 26.2.10.
    """

    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"
