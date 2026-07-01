"""Unit tests for the BlueZ D-Bus client's FDDF address extraction.

Byte fixture is a real Harman FDDF (UUID 0000fddf-...) LE advertisement
service-data capture from a PartyBox 520, documented in
docs/adr/027-bluetooth-bonding-architecture.md. Never fabricated — per
project convention, protocol-adjacent byte parsing is tested against real
captures only.
"""

from __future__ import annotations

from companion.services.bluez_dbus import _PairingAgent, extract_bredr_address

# Confirmed from hardware: LE advertisement Service Data AD structure
# (AD type 0x16) under Harman's vendor UUID 0xfddf, captured from a
# PartyBox 520 in pairing mode. Bytes 11-16 are the BR/EDR public address.
FDDF_SERVICE_DATA = bytes.fromhex("202101d3e4000014040000501b6a14fd1d00010000000000")
FDDF_KNOWN_ADDRESS = "50:1B:6A:14:FD:1D"


def test_extract_bredr_address_from_real_capture() -> None:
    assert extract_bredr_address(FDDF_SERVICE_DATA) == FDDF_KNOWN_ADDRESS


def test_extract_bredr_address_returns_none_when_too_short() -> None:
    assert extract_bredr_address(FDDF_SERVICE_DATA[:10]) is None


def test_extract_bredr_address_returns_none_for_empty_payload() -> None:
    assert extract_bredr_address(b"") is None


def test_pairing_agent_class_is_constructible() -> None:
    """Regression guard: dbus-fast's @method() signature inference on
    Annotated[...]-typed Agent1 methods silently breaks if this module ever
    re-adds `from __future__ import annotations` (PEP 563 turns annotations
    into unevaluated source text, which dbus-fast cannot resolve)."""
    agent = _PairingAgent()
    assert agent.Release() is None
    assert agent.Cancel() is None
