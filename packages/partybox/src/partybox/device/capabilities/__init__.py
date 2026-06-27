"""Device capability classes."""

from .battery import BatteryCapability
from .device_info import DeviceInfoCapability
from .power import PowerCapability

__all__ = [
    "BatteryCapability",
    "DeviceInfoCapability",
    "PowerCapability",
]
