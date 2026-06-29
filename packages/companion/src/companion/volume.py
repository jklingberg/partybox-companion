"""Shared in-memory volume state for the appliance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Recognised sources of volume information, in rough authority order
# (hardware > audio service > explicit API write).  New sources — e.g.
# "airplay" — are added here as they are integrated.
VolumeSource = Literal["ble", "spotify", "airplay", "api"]


@dataclass
class VolumeState:
    """Mutable volume state updated by service integrations and REST writes.

    This is the appliance's single source of truth for logical speaker volume
    when the hardware (BLE) volume is not yet readable.  Companion updates it
    from librespot stderr, REST POST /api/v1/volume writes, and — once BLE
    volume is implemented — VolumeChangedEvent notifications from the hardware.

    GET /api/v1/volume reads this state as a fallback when BLE raises
    NotImplementedError, and returns it as the primary value once BLE
    volume is confirmed.

    See ADR-022 for the intended long-term authority model.
    """

    level: int | None = None
    source: VolumeSource | None = None

    def update(self, percent: int, source: VolumeSource) -> None:
        """Record a new volume level (0-100) and its origin."""
        self.level = percent
        self.source = source
