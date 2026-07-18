"""Top-level scanner that returns :class:`~partybox.device.PartyBoxDevice`.

This is the primary entry point for most callers::

    from partybox import Scanner

    speaker = await Scanner.find()
    if speaker is None:
        print("No PartyBox found")
        return
    async with speaker:
        await speaker.power.turn_on()

This wraps :class:`partybox.bluetooth.scanner.Scanner` (which returns raw
:class:`~partybox.bluetooth.PartyBoxCandidate` objects) and converts the
results to :class:`~partybox.device.PartyBoxDevice` instances ready to connect.
"""

from __future__ import annotations

from dataclasses import dataclass

from partybox.bluetooth.scanner import DEFAULT_SCAN_TIMEOUT
from partybox.bluetooth.scanner import Scanner as _BleScanner
from partybox.device.partybox import PartyBoxDevice


@dataclass(frozen=True)
class ScanResult:
    """Result of :meth:`Scanner.find_with_presence` — see that method."""

    device: PartyBoxDevice | None
    beacon_seen: bool


class Scanner:
    """Discovers PartyBox speakers and returns :class:`PartyBoxDevice` handles."""

    @staticmethod
    async def find(*, timeout: float = DEFAULT_SCAN_TIMEOUT) -> PartyBoxDevice | None:
        """Scan and return the strongest-signal PartyBox, or ``None``.

        The returned device is **not yet connected**. Call
        :meth:`~partybox.device.PartyBoxDevice.connect` (or use it as an async
        context manager) before accessing capabilities.

        Args:
            timeout: BLE scan duration in seconds.

        Raises:
            DiscoveryError: if the BLE scan itself cannot be performed.
        """
        candidate = await _BleScanner.find(timeout=timeout)
        if candidate is None:
            return None
        return PartyBoxDevice(candidate)

    @staticmethod
    async def discover(*, timeout: float = DEFAULT_SCAN_TIMEOUT) -> list[PartyBoxDevice]:
        """Scan and return all found PartyBox speakers, strongest-signal first.

        Args:
            timeout: BLE scan duration in seconds.

        Raises:
            DiscoveryError: if the BLE scan itself cannot be performed.
        """
        candidates = await _BleScanner.discover(timeout=timeout)
        return [PartyBoxDevice(c) for c in candidates]

    @staticmethod
    async def find_with_presence(*, timeout: float = DEFAULT_SCAN_TIMEOUT) -> ScanResult:
        """Like :meth:`find`, but also reports whether a PartyBox-family
        device's beacon was seen even when no connectable candidate was —
        see :class:`partybox.bluetooth.scanner.DiscoveryResult`. Lets a caller
        distinguish "genuinely off/out of range" from "on, but its control
        channel isn't reachable right now" without any extra scan.

        Raises:
            DiscoveryError: if the BLE scan itself cannot be performed.
        """
        result = await _BleScanner.discover_with_presence(timeout=timeout)
        device = PartyBoxDevice(result.candidates[0]) if result.candidates else None
        return ScanResult(device=device, beacon_seen=result.beacon_seen)
