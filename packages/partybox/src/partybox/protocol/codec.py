"""Encode and decode vendor protocol frames.

The frame format (confirmed from hardware captures):

    [ SOF=0xAA ][ opcode:u8 ][ length:u8 ][ payload:length bytes ]

No checksum is present in the observed frames. If future captures reveal one,
it will be added here. See docs/reverse-engineering/discoveries.md.

This module is **pure** — no I/O, no async, no side effects. Message objects
in, bytes out; bytes in, message objects out.
"""

from __future__ import annotations

from partybox.protocol.constants import (
    OPCODE_FIRMWARE_REQUEST,
    OPCODE_FIRMWARE_RESPONSE,
    OPCODE_POWER,
    SOF,
)
from partybox.protocol.messages import (
    FirmwareVersionRequest,
    FirmwareVersionResponse,
    PowerCommand,
)


def encode(message: PowerCommand | FirmwareVersionRequest) -> bytes:
    """Encode a command message to a vendor protocol frame.

    Args:
        message: the typed command to encode.

    Returns:
        Raw frame bytes ready to write to the TX characteristic.

    Raises:
        TypeError: if ``message`` is not a recognised command type.
    """
    if isinstance(message, PowerCommand):
        payload = bytes([message.state])
        return bytes([SOF, OPCODE_POWER, len(payload)]) + payload
    if isinstance(message, FirmwareVersionRequest):
        return bytes([SOF, OPCODE_FIRMWARE_REQUEST, 0])
    raise TypeError(f"cannot encode {type(message).__name__}")


def decode(raw: bytes) -> FirmwareVersionResponse | None:
    """Decode a raw notification frame into a typed response, or ``None``.

    Only response opcodes confirmed from hardware are decoded. Unknown or
    malformed frames return ``None`` so callers can skip them.

    Args:
        raw: raw bytes from the RX characteristic notification.

    Returns:
        A typed response object, or ``None`` if the frame is not recognised.
    """
    if len(raw) < 3 or raw[0] != SOF:
        return None
    opcode = raw[1]
    length = raw[2]
    payload = raw[3 : 3 + length]
    if opcode == OPCODE_FIRMWARE_RESPONSE and len(payload) >= 3:
        return FirmwareVersionResponse(major=payload[0], minor=payload[1], patch=payload[2])
    return None
