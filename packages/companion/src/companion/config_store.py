"""Persistent appliance configuration stored in data_dir/config.json.

ConfigStore is the single owner of the config file. Pass one instance to
both the portal router and the services router so they share the same file.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)


class PortalConfig(BaseModel):
    """Appliance configuration persisted across restarts."""

    spotify_connect_name: str = "PartyBox"
    spotify_bitrate: Literal[96, 160, 320] = 320
    audio_sink_address: str | None = None


class ConfigStore:
    """Read/write PortalConfig from a JSON file on disk.

    A config file that cannot be parsed must never take the appliance down:
    the Portal is the only interface most users have, so it has to come up
    even when the config is damaged (truncated write, SD-card corruption,
    manual editing). An unreadable file is quarantined with a clear log
    message and defaults are used instead.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> PortalConfig:
        if not self._path.exists():
            return PortalConfig()
        try:
            return PortalConfig.model_validate(json.loads(self._path.read_text()))
        except (json.JSONDecodeError, ValidationError, OSError) as exc:
            quarantine = self._quarantine()
            log.error(
                "config file %s is unreadable (%s); using defaults%s",
                self._path,
                exc,
                f" — original preserved at {quarantine}" if quarantine else "",
            )
            return PortalConfig()

    def write(self, cfg: PortalConfig) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(cfg.model_dump_json(indent=2))

    def reset(self) -> None:
        """Delete the config file so the next read returns factory defaults.

        Used by the factory-reset flow. Removing the file (rather than writing
        ``PortalConfig()``) keeps disk state identical to a fresh appliance
        image, where no config file exists yet. A missing file is not an error.
        """
        try:
            self._path.unlink(missing_ok=True)
        except OSError as exc:
            log.error("config reset: could not delete %s (%s)", self._path, exc)
            raise

    def _quarantine(self) -> Path | None:
        """Move the unreadable config aside so the next write starts clean.

        Preserves the damaged file for diagnosis instead of silently
        overwriting it. Returns the quarantine path, or ``None`` if the
        move failed (read-only filesystem, permissions).
        """
        target = self._path.with_name(f"{self._path.name}.corrupt-{time.time_ns()}")
        try:
            self._path.rename(target)
        except OSError:
            return None
        return target
