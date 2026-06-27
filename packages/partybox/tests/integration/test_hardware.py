"""Hardware integration tests — require a real PartyBox and a Bluetooth adapter.

Never run in CI. Run from the Pi with::

    uv run pytest packages/partybox/ -m hardware -v

Discovery is by scanning LE for the PartyBox advertised name, so these tests
need a powered-on speaker in range but no preconfigured address.
"""

import pytest
from partybox import Scanner
from partybox.device.partybox import PartyBoxDevice

pytestmark = pytest.mark.hardware


async def test_discover_finds_a_partybox() -> None:
    devices = await Scanner.discover(timeout=10.0)
    assert devices, "no PartyBox discovered — is it powered on and in range?"
    assert all(isinstance(d, PartyBoxDevice) for d in devices)


async def test_find_returns_a_device() -> None:
    device = await Scanner.find(timeout=10.0)
    assert device is not None, "no PartyBox found"
    assert isinstance(device, PartyBoxDevice)


async def test_connect_and_disconnect() -> None:
    device = await Scanner.find(timeout=10.0)
    assert device is not None, "no PartyBox found"
    await device.connect()
    try:
        assert device.is_connected
    finally:
        await device.disconnect()
    assert not device.is_connected


async def test_context_manager() -> None:
    device = await Scanner.find(timeout=10.0)
    assert device is not None, "no PartyBox found"
    async with device:
        assert device.is_connected
    assert not device.is_connected


async def test_power_turn_on() -> None:
    """Power-on is idempotent when the speaker is already on."""
    device = await Scanner.find(timeout=10.0)
    assert device is not None, "no PartyBox found"
    async with device:
        await device.power.turn_on()  # must not raise


async def test_device_info_manufacturer() -> None:
    device = await Scanner.find(timeout=10.0)
    assert device is not None, "no PartyBox found"
    async with device:
        manufacturer = await device.device_info.manufacturer()
        assert manufacturer == "JBL"


async def test_device_info_firmware_version() -> None:
    """Firmware version is confirmed via opcode 0x21 on a PartyBox 520."""
    device = await Scanner.find(timeout=10.0)
    assert device is not None, "no PartyBox found"
    async with device:
        firmware = await device.device_info.firmware_version()
        assert firmware, "firmware_version should be non-empty"
        # Expect major.minor.patch format
        parts = firmware.split(".")
        assert len(parts) == 3, f"unexpected firmware format: {firmware!r}"
        assert all(p.isdigit() for p in parts)


@pytest.mark.xfail(
    reason=(
        "model opcode not yet confirmed: only appears in power-off TLV state dump "
        "(opcode 0x12, tag 0x40) — no request opcode found yet; "
        "see docs/reverse-engineering/open-questions.md"
    ),
    strict=True,
)
async def test_device_info_model() -> None:
    device = await Scanner.find(timeout=10.0)
    assert device is not None, "no PartyBox found"
    async with device:
        model = await device.device_info.model()
        assert model, "model should be non-empty"


async def test_battery_none_on_mains_powered_model() -> None:
    """PartyBox 520 is mains-powered; battery capability should be absent."""
    device = await Scanner.find(timeout=10.0)
    assert device is not None, "no PartyBox found"
    async with device:
        # This assertion is model-specific. On a portable model (110/310),
        # battery will not be None — adjust the test accordingly.
        # On the PartyBox 520 (primary test device) battery is expected to be None.
        assert (
            device.battery is None
        ), "PartyBox 520 is mains-powered — update this assertion for a portable model"
