"""Unit tests for the BlueZ D-Bus client's FDDF address extraction.

Byte fixture is a real Harman FDDF (UUID 0000fddf-...) LE advertisement
service-data capture from a PartyBox 520, documented in
docs/adr/027-bluetooth-bonding-architecture.md. Never fabricated — per
project convention, protocol-adjacent byte parsing is tested against real
captures only.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from companion.services.bluez_dbus import (
    BluezClient,
    _PairingAgent,
    extract_bredr_address,
    parse_fddf_payload,
)

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


# ---------------------------------------------------------------------------
# wait_for_device() — direct tests via fake D-Bus proxies
# ---------------------------------------------------------------------------

_DEVICE_PATH = "/org/bluez/hci0/dev_50_1B_6A_14_FD_1D"


class _FakeObjectManagerIface:
    """Fake org.freedesktop.DBus.ObjectManager proxy interface.

    Implements the dynamic dbus-fast attribute surface wait_for_device()
    touches: on_/off_interfaces_added and call_get_managed_objects.
    """

    def __init__(self, existing: dict[str, dict[str, dict[str, object]]]) -> None:
        self.existing = existing
        self.callbacks: list[Callable[..., None]] = []

    def on_interfaces_added(self, cb: Callable[..., None]) -> None:
        self.callbacks.append(cb)

    def off_interfaces_added(self, cb: Callable[..., None]) -> None:
        self.callbacks.remove(cb)

    async def call_get_managed_objects(self) -> dict[str, dict[str, dict[str, object]]]:
        return self.existing


class _FakeAdapterIface:
    """Fake org.bluez.Adapter1 proxy interface recording discovery calls."""

    def __init__(self, on_start: Callable[[], None] | None = None) -> None:
        self.discovery_filter: dict[str, object] | None = None
        self.started = False
        self.stopped = False
        self._on_start = on_start

    async def call_set_discovery_filter(self, f: dict[str, object]) -> None:
        self.discovery_filter = f

    async def call_start_discovery(self) -> None:
        self.started = True
        if self._on_start is not None:
            self._on_start()

    async def call_stop_discovery(self) -> None:
        self.stopped = True


class _FakeProxyObject:
    def __init__(self, iface: object) -> None:
        self._iface = iface

    def get_interface(self, name: str) -> object:
        return self._iface


class _WaitTestClient(BluezClient):
    """BluezClient with the D-Bus proxy layer replaced by fakes."""

    def __init__(self, objmgr: _FakeObjectManagerIface, adapter: _FakeAdapterIface) -> None:
        super().__init__()
        self._objmgr = objmgr
        self._adapter_iface = adapter

    async def _proxy(self, path: str, bus_name: str = "org.bluez") -> Any:  # noqa: ANN401
        return _FakeProxyObject(self._objmgr)

    async def _adapter(self) -> Any:  # noqa: ANN401
        return self._adapter_iface


async def test_wait_for_device_returns_true_when_object_already_exists() -> None:
    objmgr = _FakeObjectManagerIface({_DEVICE_PATH: {"org.bluez.Device1": {}}})
    adapter = _FakeAdapterIface()
    client = _WaitTestClient(objmgr, adapter)

    assert await client.wait_for_device(FDDF_KNOWN_ADDRESS, timeout=1.0) is True
    # No discovery needed; signal handler cleaned up either way.
    assert adapter.started is False
    assert objmgr.callbacks == []


async def test_wait_for_device_resolves_on_interfaces_added() -> None:
    objmgr = _FakeObjectManagerIface({})

    def fire() -> None:
        for cb in list(objmgr.callbacks):
            cb("/org/bluez/hci0/dev_AA_AA_AA_AA_AA_AA", {"org.bluez.Device1": {}})  # ignored
            cb(_DEVICE_PATH, {"org.bluez.Device1": {}})  # target

    adapter = _FakeAdapterIface(on_start=fire)
    client = _WaitTestClient(objmgr, adapter)

    assert await client.wait_for_device(FDDF_KNOWN_ADDRESS, timeout=1.0) is True
    assert adapter.discovery_filter is not None  # BR/EDR transport filter set
    assert adapter.started is True
    assert adapter.stopped is True  # discovery stopped in finally
    assert objmgr.callbacks == []  # signal handler unsubscribed


async def test_wait_for_device_times_out_and_cleans_up() -> None:
    objmgr = _FakeObjectManagerIface({})
    adapter = _FakeAdapterIface()
    client = _WaitTestClient(objmgr, adapter)

    assert await client.wait_for_device(FDDF_KNOWN_ADDRESS, timeout=0.05) is False
    assert adapter.started is True
    assert adapter.stopped is True
    assert objmgr.callbacks == []


# ---------------------------------------------------------------------------
# parse_fddf_payload() — live-state fields
# ---------------------------------------------------------------------------
#
# Real btmon captures from a PartyBox 520 (2026-07-16), taken while toggling a
# phone's Bluetooth connection — the same session documented in
# docs/reverse-engineering/protocol.md § "FDDF Advertisement".

# Phone also connected to the speaker (idle or playing — payload identical):
FDDF_PHONE_CONNECTED = bytes.fromhex("202101d453e2c70c06586b501b6a14fd1d00090000000000")
# Phone disconnected — companion is the only source:
FDDF_COMPANION_ONLY = bytes.fromhex("202101d453e2c70c05586b501b6a14fd1d00010000000000")


def test_parse_fddf_payload_phone_connected() -> None:
    payload = parse_fddf_payload(FDDF_PHONE_CONNECTED)
    assert payload is not None
    assert payload.bredr_address == FDDF_KNOWN_ADDRESS
    assert payload.battery_percent == 0x53  # 83%, matched the API reading
    assert payload.source_count == 0x06
    assert payload.connection_bits == 0x09


def test_parse_fddf_payload_companion_only() -> None:
    payload = parse_fddf_payload(FDDF_COMPANION_ONLY)
    assert payload is not None
    assert payload.bredr_address == FDDF_KNOWN_ADDRESS
    assert payload.source_count == 0x05
    assert payload.connection_bits == 0x01


def test_parse_fddf_payload_pairing_mode_capture() -> None:
    """The ADR-027 pairing-mode capture: byte 4 has bit 7 set with the low
    bits reading 100% (likely a charging flag — masked off), and the source
    count is 0x04 with nothing connected."""
    payload = parse_fddf_payload(FDDF_SERVICE_DATA)
    assert payload is not None
    assert payload.bredr_address == FDDF_KNOWN_ADDRESS
    assert payload.battery_percent == 0x64  # 0xe4 & 0x7f
    assert payload.source_count == 0x04
    assert payload.connection_bits == 0x01


def test_parse_fddf_payload_returns_none_when_too_short() -> None:
    assert parse_fddf_payload(FDDF_PHONE_CONNECTED[:18]) is None
    assert parse_fddf_payload(b"") is None
