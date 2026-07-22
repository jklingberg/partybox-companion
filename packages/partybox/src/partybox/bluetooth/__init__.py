"""Bluetooth control transport and discovery.

The public surface is domain-oriented and free of any ``bleak`` types:

* :class:`Scanner` finds speakers and returns :class:`PartyBoxCandidate`.
* ``candidate.connect()`` yields a connected :class:`ControlTransport`.
* :class:`MockTransport` is the in-process fake for tests.

``BleakTransport`` is the concrete BLE implementation; most callers never need
it directly — discover via :class:`Scanner` instead.
"""

from .bleak_transport import (
    CONTROL_SERVICE_UUID,
    RX_CHAR_UUID,
    TX_CHAR_UUID,
    BleakTransport,
)
from .mock import MockTransport
from .scanner import DiscoveryError, DiscoveryResult, PartyBoxCandidate, Scanner
from .transport import (
    BluetoothError,
    ConfirmedDisconnectError,
    ConnectionFailedError,
    ConnectionLostError,
    ControlTransport,
    NotConnectedError,
)

__all__ = [
    "CONTROL_SERVICE_UUID",
    "RX_CHAR_UUID",
    "TX_CHAR_UUID",
    "BleakTransport",
    "BluetoothError",
    "ConfirmedDisconnectError",
    "ConnectionFailedError",
    "ConnectionLostError",
    "ControlTransport",
    "DiscoveryError",
    "DiscoveryResult",
    "MockTransport",
    "NotConnectedError",
    "PartyBoxCandidate",
    "Scanner",
]
