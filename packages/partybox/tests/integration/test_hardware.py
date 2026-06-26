"""Hardware integration tests — require a real PartyBox and a Bluetooth adapter.

Never run in CI. Run locally with::

    uv run pytest packages/partybox/ -m hardware -v

Discovery is by scanning LE for the PartyBox advertised name, so these need a
powered-on speaker in range but no address configuration. Connecting via a
discovered candidate uses the live device handle, avoiding the speaker's
rotating private address.
"""

import pytest
from partybox.bluetooth import ControlTransport, PartyBoxCandidate, Scanner

pytestmark = pytest.mark.hardware

# Power on — verified on a PartyBox 520. See docs/reverse-engineering.
POWER_ON = bytes.fromhex("AA030105")


async def test_discover_finds_a_partybox() -> None:
    candidates = await Scanner.discover(timeout=10.0)
    assert candidates, "no PartyBox discovered — is it powered on and in range?"
    assert all(isinstance(c, PartyBoxCandidate) for c in candidates)
    assert all("PartyBox" in c.name for c in candidates)


async def test_find_connect_and_disconnect() -> None:
    speaker = await Scanner.find(timeout=10.0)
    assert speaker is not None, "no PartyBox found"
    transport = await speaker.connect()
    assert isinstance(transport, ControlTransport)
    try:
        assert transport.is_connected
    finally:
        await transport.disconnect()
    assert not transport.is_connected


async def test_write_power_on() -> None:
    speaker = await Scanner.find(timeout=10.0)
    assert speaker is not None, "no PartyBox found"
    transport = await speaker.connect()
    try:
        await transport.write(POWER_ON)  # must not raise (write-with-response)
    finally:
        await transport.disconnect()
