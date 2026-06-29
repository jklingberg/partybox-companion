"""Unit tests for ProvisioningService — parsing, state logic, and error classification.

All tests use helpers that operate on plain data without invoking nmcli.
"""

from __future__ import annotations

from companion.services.provisioning import (
    ProvisioningFailureReason,
    ProvisioningService,
    ProvisioningState,
    WifiNetwork,
    _classify_nmcli_error,
    _parse_wifi_list,
)

# ---------------------------------------------------------------------------
# _parse_wifi_list
# ---------------------------------------------------------------------------


def _make_output(*groups: dict[str, str]) -> str:
    """Build nmcli multiline -t output from a sequence of field dicts."""
    parts: list[str] = []
    for g in groups:
        for k, v in g.items():
            parts.append(f"{k}:{v}")
        parts.append("")
    return "\n".join(parts)


def test_parse_empty() -> None:
    assert _parse_wifi_list("") == []


def test_parse_single_network() -> None:
    output = _make_output({"SSID": "HomeNet", "SIGNAL": "72", "SECURITY": "WPA2"})
    result = _parse_wifi_list(output)
    assert result == [WifiNetwork(ssid="HomeNet", signal=72, security="WPA2")]


def test_parse_open_network_normalises_dashes() -> None:
    output = _make_output({"SSID": "GuestNet", "SIGNAL": "40", "SECURITY": "--"})
    result = _parse_wifi_list(output)
    assert result == [WifiNetwork(ssid="GuestNet", signal=40, security="")]


def test_parse_sorts_by_signal_descending() -> None:
    output = _make_output(
        {"SSID": "Weak", "SIGNAL": "20", "SECURITY": "WPA2"},
        {"SSID": "Strong", "SIGNAL": "90", "SECURITY": "WPA2"},
        {"SSID": "Mid", "SIGNAL": "55", "SECURITY": "WPA2"},
    )
    result = _parse_wifi_list(output)
    assert [n.ssid for n in result] == ["Strong", "Mid", "Weak"]


def test_parse_deduplicates_ssid_keeps_first() -> None:
    # nmcli may report the same SSID multiple times (different BSSIDs).
    # The first occurrence (highest signal, since nmcli sorts by signal) is kept.
    output = _make_output(
        {"SSID": "HomeNet", "SIGNAL": "80", "SECURITY": "WPA2"},
        {"SSID": "HomeNet", "SIGNAL": "30", "SECURITY": "WPA2"},
    )
    result = _parse_wifi_list(output)
    assert len(result) == 1
    assert result[0].signal == 80


def test_parse_skips_empty_ssid() -> None:
    output = _make_output(
        {"SSID": "", "SIGNAL": "60", "SECURITY": "WPA2"},
        {"SSID": "RealNet", "SIGNAL": "50", "SECURITY": "WPA2"},
    )
    result = _parse_wifi_list(output)
    assert len(result) == 1
    assert result[0].ssid == "RealNet"


def test_parse_colon_in_ssid() -> None:
    # SSID values may contain colons; partition on the first colon only.
    output = _make_output({"SSID": "Net:With:Colons", "SIGNAL": "65", "SECURITY": "WPA2"})
    result = _parse_wifi_list(output)
    assert result == [WifiNetwork(ssid="Net:With:Colons", signal=65, security="WPA2")]


def test_parse_invalid_signal_defaults_to_zero() -> None:
    output = _make_output({"SSID": "FlakyNet", "SIGNAL": "N/A", "SECURITY": "WPA2"})
    result = _parse_wifi_list(output)
    assert result == [WifiNetwork(ssid="FlakyNet", signal=0, security="WPA2")]


# ---------------------------------------------------------------------------
# _classify_nmcli_error
# ---------------------------------------------------------------------------


def test_classify_wrong_password() -> None:
    stderr = "Error: Connection activation failed: (7) Secrets were required, but not provided."
    assert _classify_nmcli_error(stderr) == ProvisioningFailureReason.AUTHENTICATION_FAILED


def test_classify_supplicant_failure() -> None:
    stderr = "Error: Connection activation failed: (8) 802.1x supplicant failed."
    assert _classify_nmcli_error(stderr) == ProvisioningFailureReason.AUTHENTICATION_FAILED


def test_classify_ssid_not_found() -> None:
    stderr = "Error: No Wi-Fi network with SSID 'MyNet' found."
    assert _classify_nmcli_error(stderr) == ProvisioningFailureReason.NOT_FOUND


def test_classify_unknown_error() -> None:
    stderr = "Error: some completely unrecognised nmcli error output"
    assert _classify_nmcli_error(stderr) == ProvisioningFailureReason.UNKNOWN


def test_classify_empty_stderr() -> None:
    assert _classify_nmcli_error("") == ProvisioningFailureReason.UNKNOWN


# ---------------------------------------------------------------------------
# ProvisioningService initial state
# ---------------------------------------------------------------------------


def test_service_initial_state() -> None:
    svc = ProvisioningService()
    status = svc.status
    assert status.state == ProvisioningState.UNPROVISIONED
    assert status.ap_ip is None
    assert status.reason is None
    assert status.message is None


def test_service_ap_ip_only_when_ap_active() -> None:
    svc = ProvisioningService()
    svc._state = ProvisioningState.AP_ACTIVE
    assert svc.status.ap_ip == "10.42.0.1"

    svc._state = ProvisioningState.CONNECTED
    assert svc.status.ap_ip is None


def test_service_reason_and_message_after_failure() -> None:
    svc = ProvisioningService()
    svc._state = ProvisioningState.AP_ACTIVE
    svc._reason = ProvisioningFailureReason.AUTHENTICATION_FAILED
    svc._message = "Incorrect WiFi password."

    status = svc.status
    assert status.reason == ProvisioningFailureReason.AUTHENTICATION_FAILED
    assert status.message == "Incorrect WiFi password."


def test_service_reason_cleared_when_not_failing() -> None:
    svc = ProvisioningService()
    # reason and message are None by default
    assert svc.status.reason is None
    assert svc.status.message is None


def test_service_custom_interface() -> None:
    svc = ProvisioningService(wifi_interface="wlan1")
    assert svc._iface == "wlan1"
