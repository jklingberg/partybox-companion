"""Unit tests for the protocol codec.

Byte fixtures are taken from real hardware captures documented in
docs/reverse-engineering/discoveries.md. Never fabricated.
"""

import pytest
from partybox.protocol.codec import decode, encode
from partybox.protocol.messages import (
    FirmwareVersionRequest,
    FirmwareVersionResponse,
    PowerCommand,
    PowerState,
)

# Confirmed from hardware: written to TX characteristic on a PartyBox 520.
POWER_ON_FRAME = bytes.fromhex("AA030105")
POWER_OFF_FRAME = bytes.fromhex("AA030104")

# Confirmed from hardware: response to AA 21 00 on a PartyBox 520 running
# firmware 26.2.10. Captured 2026-06-27.
FIRMWARE_REQUEST_FRAME = bytes.fromhex("AA2100")
FIRMWARE_RESPONSE_FRAME = bytes.fromhex("AA22041a020a00")


def test_encode_power_on() -> None:
    assert encode(PowerCommand(PowerState.ON)) == POWER_ON_FRAME


def test_encode_power_off() -> None:
    assert encode(PowerCommand(PowerState.OFF)) == POWER_OFF_FRAME


def test_encode_produces_four_bytes() -> None:
    assert len(encode(PowerCommand(PowerState.ON))) == 4
    assert len(encode(PowerCommand(PowerState.OFF))) == 4


def test_encode_starts_with_sof() -> None:
    assert encode(PowerCommand(PowerState.ON))[0] == 0xAA


def test_encode_firmware_request() -> None:
    assert encode(FirmwareVersionRequest()) == FIRMWARE_REQUEST_FRAME


def test_encode_firmware_request_three_bytes() -> None:
    assert len(encode(FirmwareVersionRequest())) == 3


def test_encode_unknown_type_raises() -> None:
    with pytest.raises(TypeError):
        encode("not a message")  # type: ignore[arg-type]


def test_decode_firmware_response() -> None:
    result = decode(FIRMWARE_RESPONSE_FRAME)
    assert isinstance(result, FirmwareVersionResponse)
    assert result.major == 26
    assert result.minor == 2
    assert result.patch == 10


def test_decode_firmware_response_str() -> None:
    result = decode(FIRMWARE_RESPONSE_FRAME)
    assert isinstance(result, FirmwareVersionResponse)
    assert str(result) == "26.2.10"


def test_decode_unknown_opcode_returns_none() -> None:
    assert decode(bytes.fromhex("AA0302AB")) is None


def test_decode_too_short_returns_none() -> None:
    assert decode(bytes.fromhex("AA22")) is None


def test_decode_wrong_sof_returns_none() -> None:
    assert decode(bytes.fromhex("BB22041a020a00")) is None
