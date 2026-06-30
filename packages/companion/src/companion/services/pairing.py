"""Bluetooth Classic A2DP pairing service.

Manages first-time pairing between the Pi and the speaker.  On a fresh
appliance image there is no stored A2DP address, so AudioService has no sink
to connect to.  PairingService handles the one-shot flow:

1. Scan for new BR/EDR devices (user puts speaker in pairing mode first).
2. Identify the speaker by name ("JBL") and address type (public = BR/EDR).
3. Run ``bluetoothctl pair / trust / connect`` to establish the link.
4. Persist the address to PortalConfig so AudioService survives reboots.
5. Hand the address to AudioService so it starts without a process restart.

Only one pairing attempt can run at a time.  Calling :meth:`start` while
a pairing is already in progress is a no-op that returns ``False``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from enum import StrEnum

from companion.config_store import ConfigStore
from companion.services.audio import AudioService

log = logging.getLogger(__name__)

_SCAN_TIMEOUT = 60.0  # seconds to wait for speaker to appear
_POLL_INTERVAL = 2.0  # seconds between bluetoothctl devices polls
_PAIR_TIMEOUT = 60.0  # seconds for the pair command (allow time for SSP confirmation)
_TRUST_TIMEOUT = 5.0
_CONNECT_TIMEOUT = 15.0

# Matches "Device AA:BB:CC:DD:EE:FF Some Name" from `bluetoothctl devices`
_DEVICE_RE = re.compile(r"^Device\s+([0-9A-F:]{17})\s+(.+)$", re.MULTILINE)


class PairingState(StrEnum):
    IDLE = "idle"
    SCANNING = "scanning"
    PAIRING = "pairing"
    FAILED = "failed"


@dataclass(frozen=True)
class PairingStatus:
    state: PairingState
    error: str | None = None


class PairingService:
    """On-demand Bluetooth Classic pairing for the A2DP audio sink.

    This is not a long-running Supervisor service.  It is an on-demand helper
    invoked by the ``POST /api/v1/audio/pair`` endpoint.  Internally it
    manages a single asyncio Task so callers do not block waiting for the
    60-second scan window.
    """

    def __init__(self, config: ConfigStore, audio: AudioService) -> None:
        self._config = config
        self._audio = audio
        self._state = PairingState.IDLE
        self._error: str | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def status(self) -> PairingStatus:
        return PairingStatus(state=self._state, error=self._error)

    async def start(self) -> bool:
        """Begin a pairing attempt in the background.

        Returns ``True`` if pairing started, ``False`` if already in progress.
        The caller should poll :attr:`status` or ``GET /api/v1/audio`` to
        track progress.
        """
        if self._task is not None and not self._task.done():
            return False
        self._error = None
        self._task = asyncio.create_task(self._do_pair(), name="pairing")
        return True

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _do_pair(self) -> None:
        self._state = PairingState.SCANNING
        # Do not filter by current_sink: when re-pairing the same speaker the
        # address is already known, and excluding it would cause the scan to
        # time out. Any JBL public-address device is a valid pairing target.
        mac = await self._find_cached_jbl()
        if mac:
            log.info("Pairing: using pre-discovered device %s — skipping scan", mac)
        else:
            log.info("Pairing: scanning for speaker (%.0fs window)", _SCAN_TIMEOUT)
            mac = await self._scan_for_jbl()

        try:
            if mac is None:
                self._state = PairingState.FAILED
                self._error = "Speaker not found. Put the speaker in pairing mode and try again."
                log.warning("Pairing: scan timed out — no JBL device found")
                return

            log.info("Pairing: found speaker at %s", mac)
            self._state = PairingState.PAIRING

            if not await self._pair(mac):
                self._state = PairingState.FAILED
                self._error = (
                    "Pairing failed. Put the speaker in pairing mode "
                    "(press the Bluetooth button once until the LEDs flash) and try again."
                )
                return

            await _btctl("trust", mac, timeout=_TRUST_TIMEOUT)
            await _btctl("connect", mac, timeout=_CONNECT_TIMEOUT)

            # Persist so AudioService survives reboots.
            cfg = self._config.read()
            self._config.write(cfg.model_copy(update={"audio_sink_address": mac}))
            log.info("Pairing: address %s saved to config", mac)

            # Wake AudioService immediately without requiring a process restart.
            self._audio.update_address(mac)

            self._state = PairingState.IDLE
            log.info("Pairing: complete")

        except asyncio.CancelledError:
            self._state = PairingState.IDLE
            raise
        except Exception as exc:
            self._state = PairingState.FAILED
            self._error = f"Unexpected error: {exc}"
            log.error("Pairing: unexpected error: %s", exc, exc_info=True)

    async def _find_cached_jbl(self) -> str | None:
        """Return MAC of any JBL BR/EDR device already cached in BlueZ."""
        devices = await _list_devices()
        for mac, name in devices.items():
            if "jbl" not in name.lower():
                continue
            if await _is_public_address(mac):
                return mac
        return None

    async def _scan_for_jbl(self) -> str | None:
        """Scan for BR/EDR devices and return the first JBL found."""
        scan_proc = await asyncio.create_subprocess_exec(
            "bluetoothctl",
            "scan",
            "on",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            deadline = asyncio.get_event_loop().time() + _SCAN_TIMEOUT
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(_POLL_INTERVAL)
                devices = await _list_devices()
                for mac, name in devices.items():
                    if "jbl" not in name.lower():
                        continue
                    if await _is_public_address(mac):
                        return mac
        finally:
            scan_proc.terminate()
            await scan_proc.wait()

        return None

    async def _pair(self, mac: str) -> bool:
        """Run ``bluetoothctl pair <mac>``. Returns True on success."""
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "bluetoothctl",
                "pair",
                mac,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_PAIR_TIMEOUT)
            except TimeoutError:
                # Kill the subprocess so BlueZ does not remain in InProgress state.
                proc.kill()
                await proc.wait()
                log.warning("Pairing: pair command timed out for %s", mac)
                return False
            output = stdout.decode(errors="replace").lower()
            log.debug("Pairing: pair output for %s: %s", mac, output.strip())
            # bluetoothctl always exits 0 regardless of success/failure, so check
            # the output text. Require explicit confirmation rather than inferring
            # success from the absence of "failed" — a silent empty output is not
            # a successful pairing.
            if "pairing successful" in output:
                return True
            # AlreadyExists means the device is already paired — treat as success.
            if "alreadyexists" in output or "already paired" in output:
                log.info("Pairing: %s was already paired", mac)
                return True
            log.warning(
                "Pairing: pair command did not confirm success for %s: %s",
                mac,
                output.strip(),
            )
            return False
        except OSError as exc:
            log.warning("Pairing: pair command error for %s: %s", mac, exc)
            return False
        finally:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------


async def _list_devices() -> dict[str, str]:
    """Return {mac: name} for all devices currently known to BlueZ."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl",
            "devices",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        return {
            m.group(1): m.group(2).strip()
            for m in _DEVICE_RE.finditer(stdout.decode(errors="replace"))
        }
    except (OSError, TimeoutError):
        return {}


async def _is_public_address(mac: str) -> bool:
    """Return True if BlueZ reports this device's address type as 'public'.

    BR/EDR (Classic Bluetooth) devices use public addresses.  LE-only devices
    that use random addresses are filtered out to avoid pairing with the
    speaker's LE GATT interface instead of the BR/EDR A2DP interface.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl",
            "info",
            mac,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        text = stdout.decode(errors="replace")
        # Line looks like: "Device AA:BB:CC:DD:EE:FF (public)"
        # or contains "AddressType: public"
        return "(public)" in text or "AddressType: public" in text
    except (OSError, TimeoutError):
        return False


async def _btctl(command: str, mac: str, *, timeout: float) -> None:
    """Run a single bluetoothctl command, ignoring errors."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl",
            command,
            mac,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except (OSError, TimeoutError) as exc:
        log.warning("Pairing: %s %s failed: %s", command, mac, exc)
