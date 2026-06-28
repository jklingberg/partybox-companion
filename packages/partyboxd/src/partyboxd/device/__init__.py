"""Daemon device layer — lifecycle management for a single PartyBox."""

from .events import ConnectedEvent, DeviceEvent, DisconnectedEvent, EventBus, PowerChangedEvent
from .manager import DeviceManager, DeviceNotConnectedError, StatusSnapshot

__all__ = [
    "ConnectedEvent",
    "DeviceEvent",
    "DeviceManager",
    "DeviceNotConnectedError",
    "DisconnectedEvent",
    "EventBus",
    "PowerChangedEvent",
    "StatusSnapshot",
]
