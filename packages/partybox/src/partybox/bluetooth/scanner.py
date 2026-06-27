"""PartyBox discovery over BLE.

:class:`Scanner` finds PartyBox speakers and returns :class:`PartyBoxCandidate`
domain objects. A candidate can be connected directly with
:meth:`PartyBoxCandidate.connect`, which yields a connected
:class:`~partybox.bluetooth.transport.ControlTransport`.

This is where the speaker's BLE identity complexity is hidden. The PartyBox
advertises with a rapidly-rotating private address; a candidate captures the
*live* device handle from the scan, so connecting uses it directly rather than
re-resolving a soon-stale address (see ADR-015). ``bleak`` types never escape
this module — callers see only ``PartyBoxCandidate`` and ``ControlTransport``.
"""

from __future__ import annotations

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from .bleak_transport import BleakTransport
from .transport import BluetoothError, ControlTransport

DEFAULT_SCAN_TIMEOUT = 8.0

# Advertised name substring shared by every PartyBox model (520, 310, …).
_PARTYBOX_NAME = "PartyBox"

_TRANSPORT_ERRORS = (BleakError, OSError)


class DiscoveryError(BluetoothError):
    """Raised when a BLE scan cannot be performed."""


class PartyBoxCandidate:
    """A PartyBox found during a scan.

    Holds the live device handle internally so :meth:`connect` can bind to it
    directly. The ``address`` is informational; it is a rotating private
    address and should not be persisted.
    """

    def __init__(self, name: str, address: str, rssi: int | None, device: BLEDevice) -> None:
        self.name = name
        self.address = address
        self.rssi = rssi
        self._device = device

    async def connect(self) -> ControlTransport:
        """Open and return a connected control transport to this speaker.

        Raises:
            ConnectionFailedError: if the connection could not be established.
        """
        transport = BleakTransport._for_device(self._device)
        await transport.connect()
        return transport

    def __repr__(self) -> str:
        return f"PartyBoxCandidate(name={self.name!r}, address={self.address!r}, rssi={self.rssi})"


class Scanner:
    """Discovers PartyBox speakers over BLE."""

    @staticmethod
    async def discover(*, timeout: float = DEFAULT_SCAN_TIMEOUT) -> list[PartyBoxCandidate]:
        """Scan for PartyBox speakers.

        Args:
            timeout: how long to scan, in seconds.

        Returns:
            Candidates whose advertised name marks them as a PartyBox, strongest
            signal first.

        Raises:
            DiscoveryError: if the scan could not be performed (no adapter,
                insufficient privileges, etc.).
        """
        try:
            found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        except _TRANSPORT_ERRORS as exc:
            raise DiscoveryError(f"BLE scan failed: {exc}") from exc

        candidates: list[PartyBoxCandidate] = []
        for device, adv in found.values():
            name = adv.local_name or device.name
            if not name or _PARTYBOX_NAME not in name:
                continue
            candidates.append(
                PartyBoxCandidate(name=name, address=device.address, rssi=adv.rssi, device=device)
            )
        candidates.sort(key=lambda c: (c.rssi is None, -(c.rssi or 0)))
        return candidates

    @staticmethod
    async def find(*, timeout: float = DEFAULT_SCAN_TIMEOUT) -> PartyBoxCandidate | None:
        """Return the strongest-signal PartyBox found, or ``None``."""
        candidates = await Scanner.discover(timeout=timeout)
        return candidates[0] if candidates else None
