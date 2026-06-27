"""Smoke test: the package imports cleanly and exposes the expected public API."""

import partybox


def test_package_imports() -> None:
    assert partybox is not None


def test_public_api_names() -> None:
    expected = {
        "Scanner",
        "PartyBoxDevice",
        "PowerCapability",
        "DeviceInfoCapability",
        "BatteryCapability",
        "BluetoothError",
        "ConnectionFailedError",
        "ConnectionLostError",
        "NotConnectedError",
        "DiscoveryError",
        "__version__",
    }
    assert expected <= set(partybox.__all__)
