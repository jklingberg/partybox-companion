"""Shared in-memory volume state for the appliance."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class VolumeState:
    """Mutable volume state updated by service integrations and REST writes.

    This is the appliance's single source of truth for logical speaker volume
    when the hardware (BLE) volume is not yet readable. Companion updates it
    from librespot stderr and REST POST /api/v1/volume writes; GET /api/v1/volume
    reads from it as a fallback when BLE raises NotImplementedError.
    """

    level: int | None = None

    def update(self, percent: int) -> None:
        """Record a new volume level (0-100)."""
        self.level = percent
