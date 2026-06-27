"""Device model: PartyBoxDevice and capability classes."""

from .capabilities import BatteryCapability, DeviceInfoCapability, PowerCapability
from .partybox import PartyBoxDevice

__all__ = [
    "BatteryCapability",
    "DeviceInfoCapability",
    "PartyBoxDevice",
    "PowerCapability",
]
