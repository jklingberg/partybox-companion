"""Unit tests for BatteryCapability using MockTransport.

Battery status is read via the vendor protocol (opcode 0x9D → 0x9E). The
response fixtures are real captures from a JBL PartyBox 520 (2026-07-05) — one
on battery, one on mains. See docs/reverse-engineering/open-questions.md.
"""

import pytest
from partybox.bluetooth.mock import MockTransport
from partybox.device.capabilities.battery import BatteryCapability
from partybox.protocol.codec import encode
from partybox.protocol.messages import BatteryStatusRequest, ChargingStatus

BATTERY_REQUEST = encode(BatteryStatusRequest())

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


async def test_level_returns_derived_percentage() -> None:
    transport = MockTransport()
    transport.stub(BATTERY_REQUEST, BATTERY_RESPONSE_ON_BATTERY)
    async with transport:
        cap = BatteryCapability(transport)
        assert await cap.level() == 90  # 4296 / 4786


async def test_status_full_reading_on_battery() -> None:
    transport = MockTransport()
    transport.stub(BATTERY_REQUEST, BATTERY_RESPONSE_ON_BATTERY)
    async with transport:
        status = await BatteryCapability(transport).status()
        assert status.battery_id == "HP0006-CP0041242"
        assert status.charging_status is ChargingStatus.DISCHARGING
        assert status.remaining_playtime_minutes == 714
        assert status.charge_percent == 90


async def test_status_on_mains() -> None:
    transport = MockTransport()
    transport.stub(BATTERY_REQUEST, BATTERY_RESPONSE_ON_MAINS)
    async with transport:
        status = await BatteryCapability(transport).status()
        assert status.charging_status is ChargingStatus.CHARGING
        assert status.remaining_playtime_minutes is None
        assert status.charge_percent == 91


async def test_status_ignores_unrelated_notifications() -> None:
    transport = MockTransport()
    async with transport:
        transport.feed(bytes.fromhex("AA120400530100"))  # 0x12 state dump
        transport.feed(BATTERY_RESPONSE_ON_BATTERY)
        assert await BatteryCapability(transport).level() == 90


async def test_status_reassembles_fragmented_response() -> None:
    # A full 0x9E reading exceeds a small ATT MTU and arrives as two
    # notifications. The first fragment stops before the CHARGING_STATUS TLV,
    # so decoding it alone would drop that field; reassembly recovers it.
    transport = MockTransport()
    async with transport:
        transport.feed(BATTERY_RESPONSE_ON_MAINS[:24])
        transport.feed(BATTERY_RESPONSE_ON_MAINS[24:])
        status = await BatteryCapability(transport).status()
        assert status.charging_status is ChargingStatus.CHARGING
        assert status.charge_percent == 91


async def test_level_raises_without_capacity() -> None:
    # A 0x9E response carrying only charging_status has no capacity to derive %.
    transport = MockTransport()
    transport.stub(BATTERY_REQUEST, bytes.fromhex("AA9E03090101"))
    async with transport:
        with pytest.raises(RuntimeError):
            await BatteryCapability(transport).level()
