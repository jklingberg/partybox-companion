"""Unit tests for PowerCapability using MockTransport."""

from partybox.bluetooth.mock import MockTransport
from partybox.device.capabilities.power import PowerCapability

POWER_ON_FRAME = bytes.fromhex("AA030105")
POWER_OFF_FRAME = bytes.fromhex("AA030104")


async def test_turn_on_writes_correct_frame() -> None:
    transport = MockTransport()
    async with transport:
        cap = PowerCapability(transport)
        await cap.turn_on()
    assert transport.writes == [POWER_ON_FRAME]


async def test_turn_off_writes_correct_frame() -> None:
    transport = MockTransport()
    async with transport:
        cap = PowerCapability(transport)
        await cap.turn_off()
    assert transport.writes == [POWER_OFF_FRAME]


async def test_turn_on_then_off_writes_both_frames() -> None:
    transport = MockTransport()
    async with transport:
        cap = PowerCapability(transport)
        await cap.turn_on()
        await cap.turn_off()
    assert transport.writes == [POWER_ON_FRAME, POWER_OFF_FRAME]
