"""Shared volume state for the appliance.

:class:`VolumeState` is a lightweight, thread-safe-ish container for the
most recently known playback volume (0-100). It sits between the sources
(librespot, AirPlay, future BLE) and the consumers (REST API, Portal).

The volume is intentionally stored in one place — not inside any individual
service — so that every surface sees the same value regardless of which audio
source is currently active.

Usage::

    state = VolumeState()

    # From SpotifyService when librespot reports a volume change:
    state.update(72)

    # From the REST API when reading current volume:
    level = state.level   # 72, or None if not yet known
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class VolumeState:
    """In-memory holder for the most recently known volume level.

    ``level`` is ``None`` until at least one source reports a value.
    """

    _level: int | None = field(default=None, init=False, repr=False)

    @property
    def level(self) -> int | None:
        """Most recently known volume (0-100), or ``None`` if not yet known."""
        return self._level

    def update(self, percent: int) -> None:
        """Record a new volume reading from any audio source.

        Args:
            percent: the new volume level, 0-100.

        Raises:
            ValueError: if *percent* is outside 0-100.
        """
        if not 0 <= percent <= 100:
            raise ValueError(f"volume percent must be 0-100, got {percent!r}")
        self._level = percent
