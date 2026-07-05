"""Unit tests for the protocol codec.

Byte fixtures are taken from real hardware captures documented in
docs/reverse-engineering/discoveries.md. Never fabricated.
"""

import pytest
from partybox.protocol.codec import decode, encode
from partybox.protocol.messages import (
    BatteryFeature,
    BatteryStatusRequest,
    BatteryStatusResponse,
    ChargingStatus,
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


# --- Battery status (opcode 0x9D request / 0x9E response) -------------------
#
# Both response frames are real captures from a JBL PartyBox 520 (2026-07-05),
# requested with AA 9D 0C 01..0C. The only difference between them is the power
# source: the first was captured on battery, the second on mains.
BATTERY_REQUEST_FRAME = bytes.fromhex("AA9D0C0102030405060708090A0B0C")

BATTERY_RESPONSE_ON_BATTERY = bytes.fromhex(
    "aa9e3f01104850303030362d4350303034313234320202ca02030200000402c810"
    "0502b21206025c1207020100080163090102"
    "0a01000b04e00d00000c04cc060000"
)
BATTERY_RESPONSE_ON_MAINS = bytes.fromhex(
    "aa9e3f01104850303030362d4350303034313234320202ffff030200000402fd10"
    "0502b21206025c1207020100080163090101"
    "0a01000b04e80d00000c04cc060000"
)
# Real capture on mains once fully charged (remaining == full capacity):
# CHARGING_STATUS reads 3, distinct from the 1 seen while still charging.
BATTERY_RESPONSE_ON_MAINS_FULL = bytes.fromhex(
    "aa9e3f01104850303030362d43503030343132343202"
    "02ffff0302000004029312050293120602"
    "5c1207020100080163090103"
    "0a01000b040f0e00000c04d1060000"
)


def test_encode_battery_request_default_all_features() -> None:
    assert encode(BatteryStatusRequest()) == BATTERY_REQUEST_FRAME


def test_encode_battery_request_subset() -> None:
    frame = encode(BatteryStatusRequest(features=(BatteryFeature.CHARGING_STATUS,)))
    assert frame == bytes.fromhex("AA9D0109")


def test_decode_battery_on_battery() -> None:
    result = decode(BATTERY_RESPONSE_ON_BATTERY)
    assert isinstance(result, BatteryStatusResponse)
    assert result.battery_id == "HP0006-CP0041242"
    assert result.remaining_playtime_minutes == 714
    assert result.temperature_max == 0
    assert result.remaining_capacity_mah == 4296
    assert result.full_charge_capacity_mah == 4786
    assert result.design_capacity_mah == 4700
    assert result.cycle_count == 1
    assert result.state_of_health_percent == 99
    assert result.charging_status is ChargingStatus.DISCHARGING
    assert result.total_power_on_minutes == 3552
    assert result.total_playback_minutes == 1740


def test_decode_battery_on_battery_derived_charge_percent() -> None:
    result = decode(BATTERY_RESPONSE_ON_BATTERY)
    assert isinstance(result, BatteryStatusResponse)
    # 4296 / 4786 ≈ 89.76 → 90
    assert result.charge_percent == 90


def test_decode_battery_on_mains_charging_status() -> None:
    result = decode(BATTERY_RESPONSE_ON_MAINS)
    assert isinstance(result, BatteryStatusResponse)
    assert result.charging_status is ChargingStatus.CHARGING
    # 4349 / 4786 ≈ 90.87 → 91
    assert result.charge_percent == 91


def test_decode_battery_on_mains_full_charging_status() -> None:
    result = decode(BATTERY_RESPONSE_ON_MAINS_FULL)
    assert isinstance(result, BatteryStatusResponse)
    # Fully charged on mains reads CHARGING_STATUS 3 (FULL), still "on mains".
    assert result.charging_status is ChargingStatus.FULL
    assert result.charging_status.on_mains is True
    assert result.charge_percent == 100  # 4755 / 4755


def test_decode_battery_on_mains_playtime_sentinel_is_none() -> None:
    result = decode(BATTERY_RESPONSE_ON_MAINS)
    assert isinstance(result, BatteryStatusResponse)
    # 0xFFFF is the "not meaningful" sentinel on mains — surfaced as None.
    assert result.remaining_playtime_minutes is None


def test_decode_battery_ignores_unknown_feature_id() -> None:
    # feature id 0x7F is unknown; a known field (charging_status=1) still parses.
    frame = bytes.fromhex("AA9E067F0100090101")
    result = decode(frame)
    assert isinstance(result, BatteryStatusResponse)
    assert result.charging_status is ChargingStatus.CHARGING
