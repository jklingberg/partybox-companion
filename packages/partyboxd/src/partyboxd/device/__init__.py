"""Daemon device layer — lifecycle management for a single PartyBox."""

from .manager import DeviceManager, StatusSnapshot

__all__ = ["DeviceManager", "StatusSnapshot"]
