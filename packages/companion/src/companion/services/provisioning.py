"""WiFi provisioning service — manages the NetworkManager AP lifecycle.

On boot, the service checks whether the appliance has an active WiFi STA
connection. If so, it exits immediately and normal operation continues. If
not, it creates a temporary open access point ("PartyBox Companion Setup")
and waits for the user to submit credentials via the REST API.

On a single-radio Pi, the AP and STA cannot coexist. When credentials are
submitted, the AP is torn down before the STA connection is attempted. If
connection fails, the AP is re-created so the user can retry.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum

log = logging.getLogger(__name__)

_AP_SSID = "PartyBox Companion Setup"
_AP_CON_NAME = "companion-ap"
_AP_IP = "10.42.0.1"
_CONNECT_TIMEOUT = 30.0


class ProvisioningState(StrEnum):
    UNPROVISIONED = "unprovisioned"
    AP_ACTIVE = "ap_active"
    CONNECTING = "connecting"
    CONNECTED = "connected"


class ProvisioningFailureReason(StrEnum):
    AUTHENTICATION_FAILED = "authentication_failed"
    TIMEOUT = "timeout"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


_FAILURE_MESSAGES: dict[ProvisioningFailureReason, str] = {
    ProvisioningFailureReason.AUTHENTICATION_FAILED: "Incorrect WiFi password.",
    ProvisioningFailureReason.TIMEOUT: "Connection timed out. Move closer to your router and try again.",  # noqa: E501
    ProvisioningFailureReason.NOT_FOUND: "Network not found. Move closer and scan again.",
    ProvisioningFailureReason.UNKNOWN: "Could not connect. Please try again.",
}


@dataclass(frozen=True)
class WifiNetwork:
    """A visible WiFi network as reported by NetworkManager."""

    ssid: str
    signal: int  # 0-100
    security: str  # e.g. "WPA2"; empty string for open networks


@dataclass(frozen=True)
class ProvisioningStatus:
    state: ProvisioningState
    ap_ip: str | None  # non-None only when state is AP_ACTIVE
    reason: ProvisioningFailureReason | None = None
    message: str | None = None


class ProvisioningService:
    """Manages the WiFi provisioning AP lifecycle."""

    def __init__(self, wifi_interface: str = "wlan0") -> None:
        self._iface = wifi_interface
        self._state = ProvisioningState.UNPROVISIONED
        self._reason: ProvisioningFailureReason | None = None
        self._message: str | None = None
        self._connect_ssid: str | None = None
        self._connect_password: str | None = None
        self._connect_event = asyncio.Event()

    @property
    def status(self) -> ProvisioningStatus:
        ap_ip = _AP_IP if self._state == ProvisioningState.AP_ACTIVE else None
        return ProvisioningStatus(
            state=self._state,
            ap_ip=ap_ip,
            reason=self._reason,
            message=self._message,
        )

    async def request_connect(self, ssid: str, password: str | None) -> None:
        """Signal the provisioning loop to attempt a connection to *ssid*."""
        self._connect_ssid = ssid
        self._connect_password = password
        self._connect_event.set()

    async def scan_networks(self) -> list[WifiNetwork]:
        """Return visible WiFi networks, sorted by signal strength descending."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "nmcli",
                "--mode",
                "multiline",
                "-t",
                "-f",
                "SSID,SIGNAL,SECURITY",
                "device",
                "wifi",
                "list",
                "ifname",
                self._iface,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
        except OSError:
            log.warning("Provisioning: nmcli not available -- cannot scan networks")
            return []
        return _parse_wifi_list((stdout or b"").decode(errors="replace"))

    async def run(self) -> None:
        """Start the provisioning lifecycle. Returns when WiFi is connected."""
        # Delete any leftover AP from a previous run before checking state.
        # If the service restarts while the AP is active, wlan0 shows as
        # "wifi:connected" (AP mode), which would fool _is_sta_connected().
        await _nmcli("connection", "delete", _AP_CON_NAME)
        log.info("Provisioning service: checking WiFi state")
        try:
            if await self._is_sta_connected():
                log.info("WiFi already connected -- provisioning not needed")
                self._state = ProvisioningState.CONNECTED
                return
            log.info("No active WiFi connection -- entering provisioning mode")
            await self._provision()
        except asyncio.CancelledError:
            log.info("Provisioning service: cancelled -- cleaning up AP")
            await _nmcli("connection", "delete", _AP_CON_NAME)
            raise

    async def _is_sta_connected(self) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "nmcli",
                "-t",
                "-f",
                "TYPE,STATE",
                "device",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
        except OSError:
            log.warning("Provisioning: nmcli not available -- assuming WiFi connected")
            return True
        for line in (stdout or b"").decode().splitlines():
            device_type, _, state = line.partition(":")
            if device_type == "wifi" and state == "connected":
                return True
        return False

    async def _provision(self) -> None:
        await self._create_ap()
        while True:
            self._connect_event.clear()
            await self._connect_event.wait()

            ssid = self._connect_ssid
            password = self._connect_password
            if ssid is None:
                continue

            self._state = ProvisioningState.CONNECTING
            self._reason = None
            self._message = None
            log.info("Provisioning: connecting to %r", ssid)

            # Single-radio: AP and STA cannot coexist. Delete the AP first.
            await _nmcli("connection", "delete", _AP_CON_NAME)

            failure = await self._do_connect(ssid, password)
            if failure is None:
                log.info("Provisioning: joined %r -- provisioning complete", ssid)
                self._state = ProvisioningState.CONNECTED
                return

            log.warning(
                "Provisioning: connection to %r failed (%s) -- restoring AP",
                ssid,
                failure.value,
            )
            self._reason = failure
            self._message = _FAILURE_MESSAGES[failure]
            await self._create_ap()

    async def _create_ap(self) -> None:
        await _nmcli("connection", "delete", _AP_CON_NAME)
        result = await asyncio.create_subprocess_exec(
            "nmcli",
            "connection",
            "add",
            "type",
            "wifi",
            "ifname",
            self._iface,
            "con-name",
            _AP_CON_NAME,
            "ssid",
            _AP_SSID,
            "802-11-wireless.mode",
            "ap",
            "802-11-wireless.band",
            "bg",
            "ipv4.method",
            "shared",
            "ipv4.addresses",
            f"{_AP_IP}/24",
            "connection.autoconnect",
            "no",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_bytes = await result.communicate()
        if result.returncode != 0:
            stderr = (stderr_bytes or b"").decode(errors="replace").strip()
            log.error("Provisioning: failed to create AP connection: %s", stderr)
            return

        up = await asyncio.create_subprocess_exec(
            "nmcli",
            "connection",
            "up",
            _AP_CON_NAME,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_bytes = await up.communicate()
        if up.returncode != 0:
            stderr = (stderr_bytes or b"").decode(errors="replace").strip()
            log.error("Provisioning: failed to bring up AP: %s", stderr)
            return

        self._state = ProvisioningState.AP_ACTIVE
        log.info("Provisioning AP active (SSID=%r, ip=%s)", _AP_SSID, _AP_IP)

    async def _do_connect(
        self, ssid: str, password: str | None
    ) -> ProvisioningFailureReason | None:
        """Attempt to join *ssid*. Returns None on success, a reason on failure."""
        cmd = ["nmcli", "device", "wifi", "connect", ssid, "ifname", self._iface]
        if password:
            cmd += ["password", password]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            log.warning("Provisioning: cannot run nmcli: %s", exc)
            return ProvisioningFailureReason.UNKNOWN

        try:
            _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=_CONNECT_TIMEOUT)
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            log.warning("Provisioning: nmcli connect timed out after %.0fs", _CONNECT_TIMEOUT)
            return ProvisioningFailureReason.TIMEOUT

        if proc.returncode == 0:
            return None
        stderr = (stderr_bytes or b"").decode(errors="replace").strip()
        reason = _classify_nmcli_error(stderr)
        log.warning(
            "Provisioning: nmcli connect failed (rc=%d, reason=%s): %s",
            proc.returncode,
            reason.value,
            stderr,
        )
        return reason


async def _nmcli(*args: str) -> None:
    """Run an nmcli command, ignoring all errors."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "nmcli",
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
    except OSError:
        pass


def _classify_nmcli_error(stderr: str) -> ProvisioningFailureReason:
    """Classify an nmcli connection error by inspecting stderr output."""
    lower = stderr.lower()
    if any(kw in lower for kw in ("secrets", "supplicant", "802.1x", "password")):
        return ProvisioningFailureReason.AUTHENTICATION_FAILED
    if any(kw in lower for kw in ("no wi-fi network", "not found", "no network with ssid")):
        return ProvisioningFailureReason.NOT_FOUND
    return ProvisioningFailureReason.UNKNOWN


def _parse_wifi_list(output: str) -> list[WifiNetwork]:
    """Parse nmcli multiline device wifi list output.

    nmcli's multiline format puts each field on its own line as KEY:VALUE.
    Records are separated by blank lines in some modes, but not all (the
    separator depends on nmcli version and flags). Instead of relying on
    blank lines, a new record is detected when SSID appears while the
    current group already has data — SSID is always the first field
    because we request fields in SSID,SIGNAL,SECURITY order.
    """
    groups: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                groups.append(current)
                current = {}
            continue
        key, _, value = line.partition(":")
        k = key.strip()
        v = value.strip()
        if k == "SSID" and current:
            groups.append(current)
            current = {}
        current[k] = v
    if current:
        groups.append(current)

    seen: set[str] = set()
    networks: list[WifiNetwork] = []
    for g in groups:
        ssid = g.get("SSID", "")
        if not ssid or ssid == "--" or ssid in seen:
            continue
        seen.add(ssid)
        try:
            signal = int(g.get("SIGNAL", "0"))
        except ValueError:
            signal = 0
        security = g.get("SECURITY", "")
        if security == "--":
            security = ""
        networks.append(WifiNetwork(ssid=ssid, signal=signal, security=security))

    return sorted(networks, key=lambda n: n.signal, reverse=True)
