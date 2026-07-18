"""CI-safe unit tests for the BLE scanner.

These mock ``BleakScanner.discover`` so PartyBox filtering, sorting, candidate
mapping, and error handling can be tested without a Bluetooth adapter. A real
scan/connect is covered by the hardware-marked integration tests.
"""

from types import SimpleNamespace

import partybox.bluetooth as bt
import pytest
from bleak import BleakScanner
from bleak.exc import BleakError
from partybox.bluetooth import (
    ControlTransport,
    DiscoveryError,
    PartyBoxCandidate,
    Scanner,
)
from partybox.bluetooth import bleak_transport as bleak_mod
from partybox.bluetooth.scanner import HARMAN_FDDF_UUID

# Real PartyBox 520 FDDF service-data capture — see
# docs/reverse-engineering/protocol.md § "FDDF Advertisement".
_REAL_FDDF_PAYLOAD = bytes.fromhex("202101d453e2c70c05586b501b6a14fd1d00010000000000")


def _device(address: str, name: str | None) -> SimpleNamespace:
    return SimpleNamespace(address=address, name=name)


def _adv(
    local_name: str | None = None,
    rssi: int | None = None,
    service_data: dict[str, bytes] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(local_name=local_name, rssi=rssi, service_data=service_data or {})


def _patch_discover(monkeypatch: pytest.MonkeyPatch, result: object) -> None:
    async def fake_discover(**_kwargs: object) -> object:
        return result

    monkeypatch.setattr(BleakScanner, "discover", staticmethod(fake_discover))


async def test_discover_returns_only_partyboxes(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_discover(
        monkeypatch,
        {
            "a": (_device("AA", "JBL PartyBox 520"), _adv(rssi=-50)),
            "b": (_device("BB", "Some Phone"), _adv(rssi=-40)),
            "c": (_device("CC", None), _adv(local_name="JBL PartyBox 310", rssi=-60)),
        },
    )
    candidates = await Scanner.discover()
    assert [c.name for c in candidates] == ["JBL PartyBox 520", "JBL PartyBox 310"]
    assert all(isinstance(c, PartyBoxCandidate) for c in candidates)


async def test_discover_sorts_strongest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_discover(
        monkeypatch,
        {
            "a": (_device("AA", "JBL PartyBox weak"), _adv(rssi=-90)),
            "b": (_device("BB", "JBL PartyBox strong"), _adv(rssi=-40)),
            "c": (_device("CC", "JBL PartyBox unknown"), _adv(rssi=None)),
        },
    )
    rssis = [c.rssi for c in await Scanner.discover()]
    assert rssis == [-40, -90, None]


async def test_candidate_exposes_domain_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_discover(
        monkeypatch,
        {"a": (_device("AA:BB:CC:DD:EE:FF", "JBL PartyBox 520"), _adv(rssi=-55))},
    )
    (candidate,) = await Scanner.discover()
    assert candidate.name == "JBL PartyBox 520"
    assert candidate.address == "AA:BB:CC:DD:EE:FF"
    assert candidate.rssi == -55


async def test_find_returns_strongest(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_discover(
        monkeypatch,
        {
            "a": (_device("AA", "JBL PartyBox far"), _adv(rssi=-80)),
            "b": (_device("BB", "JBL PartyBox near"), _adv(rssi=-30)),
        },
    )
    found = await Scanner.find()
    assert found is not None
    assert found.name == "JBL PartyBox near"


async def test_find_returns_none_when_no_partybox(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_discover(monkeypatch, {"a": (_device("AA", "Laptop"), _adv(rssi=-40))})
    assert await Scanner.find() is None


async def test_discover_wraps_bleak_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(**_kwargs: object) -> object:
        raise BleakError("no adapter")

    monkeypatch.setattr(BleakScanner, "discover", staticmethod(boom))
    with pytest.raises(DiscoveryError):
        await Scanner.discover()


async def test_candidate_connect_opens_a_control_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bind a candidate to a fake live device, stub the actual BLE connect, and
    # confirm we get back a connected ControlTransport — no adapter needed.
    connected: list[bool] = []

    async def fake_connect(self: object) -> None:
        connected.append(True)

    monkeypatch.setattr(bleak_mod.BleakTransport, "connect", fake_connect)

    candidate = PartyBoxCandidate(
        name="JBL PartyBox 520",
        address="AA:BB:CC:DD:EE:FF",
        rssi=-50,
        device=_device("AA:BB:CC:DD:EE:FF", "JBL PartyBox 520"),  # type: ignore[arg-type]
    )
    transport = await candidate.connect()
    assert isinstance(transport, ControlTransport)
    assert transport.address == "AA:BB:CC:DD:EE:FF"
    assert connected == [True]


def test_public_api_exposes_no_bleak_types() -> None:
    # Every exported symbol must be a partybox type, never a bleak library type.
    for name in bt.__all__:
        obj = getattr(bt, name)
        module = getattr(obj, "__module__", "")
        assert not module.startswith("bleak"), f"{name} leaks a bleak type from {module}"


async def test_discover_skips_bluez_classic_device_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bonded A2DP (BR/EDR) device object carries the PartyBox name but a
    ``public`` BlueZ address type — it has no control GATT service and must
    not become a connect candidate. The LE control advert uses a rotating
    private (``random``) address and must survive the filter, as must devices
    from backends that don't expose BlueZ props at all."""
    classic = SimpleNamespace(
        address="50:1B:6A:14:FD:1D",
        name="JBL PartyBox 520",
        details={"props": {"AddressType": "public"}},
    )
    le = SimpleNamespace(
        address="72:29:68:93:69:AE",
        name="JBL PartyBox 520",
        details={"props": {"AddressType": "random"}},
    )
    no_props = _device("AA", "JBL PartyBox 310")  # non-BlueZ backend shape
    _patch_discover(
        monkeypatch,
        {
            "classic": (classic, _adv(rssi=-30)),
            "le": (le, _adv(rssi=-50)),
            "bare": (no_props, _adv(rssi=-60)),
        },
    )
    candidates = await Scanner.discover()
    assert [c.address for c in candidates] == ["72:29:68:93:69:AE", "AA"]


async def test_discover_with_presence_reports_beacon_without_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The speaker's FDDF beacon can be seen even when no connectable
    (named) candidate is found — e.g. the control advert is currently
    suppressed but the beacon, which broadcasts independently while
    powered, still is. beacon_seen must reflect that."""
    _patch_discover(
        monkeypatch,
        {
            "beacon-only": (
                _device("AA:BB:CC:DD:EE:FF", None),
                _adv(rssi=-50, service_data={HARMAN_FDDF_UUID: _REAL_FDDF_PAYLOAD}),
            ),
        },
    )
    result = await Scanner.discover_with_presence()
    assert result.candidates == []
    assert result.beacon_seen is True


async def test_discover_with_presence_false_when_nothing_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_discover(monkeypatch, {"a": (_device("AA", "Laptop"), _adv(rssi=-40))})
    result = await Scanner.discover_with_presence()
    assert result.candidates == []
    assert result.beacon_seen is False


async def test_discover_with_presence_true_alongside_a_real_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The common case: the named connectable advert AND the beacon are both
    seen in the same scan (both come from the same BleakScanner.discover()
    call, at no extra radio cost)."""
    _patch_discover(
        monkeypatch,
        {
            "a": (
                _device("AA:BB:CC:DD:EE:FF", "JBL PartyBox 520"),
                _adv(rssi=-50, service_data={HARMAN_FDDF_UUID: _REAL_FDDF_PAYLOAD}),
            ),
        },
    )
    result = await Scanner.discover_with_presence()
    assert len(result.candidates) == 1
    assert result.beacon_seen is True


async def test_discover_unaffected_by_beacon_presence(monkeypatch: pytest.MonkeyPatch) -> None:
    """discover() (no presence) must behave exactly as before — beacon-only
    entries with no connectable name still contribute nothing to it."""
    _patch_discover(
        monkeypatch,
        {
            "beacon-only": (
                _device("AA", None),
                _adv(rssi=-50, service_data={HARMAN_FDDF_UUID: _REAL_FDDF_PAYLOAD}),
            ),
        },
    )
    assert await Scanner.discover() == []


async def test_find_with_presence_wraps_top_level_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """The top-level partybox.Scanner.find_with_presence() wraps the
    candidate into a PartyBoxDevice, same as find(), while still surfacing
    beacon_seen."""
    import partybox

    _patch_discover(
        monkeypatch,
        {
            "a": (
                _device("AA:BB:CC:DD:EE:FF", "JBL PartyBox 520"),
                _adv(rssi=-50, service_data={HARMAN_FDDF_UUID: _REAL_FDDF_PAYLOAD}),
            ),
        },
    )
    result = await partybox.Scanner.find_with_presence()
    # PartyBoxDevice.address is only populated after connect() (ADR-015 —
    # the rotating private address isn't meant to be read pre-connection),
    # so this only proves the wrapping happened, not the specific address.
    assert result.device is not None
    assert result.beacon_seen is True


async def test_find_with_presence_beacon_seen_without_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact scenario this feature exists for: no connectable candidate,
    but the beacon proves the speaker is still powered."""
    import partybox

    _patch_discover(
        monkeypatch,
        {
            "beacon-only": (
                _device("AA", None),
                _adv(rssi=-50, service_data={HARMAN_FDDF_UUID: _REAL_FDDF_PAYLOAD}),
            ),
        },
    )
    result = await partybox.Scanner.find_with_presence()
    assert result.device is None
    assert result.beacon_seen is True
