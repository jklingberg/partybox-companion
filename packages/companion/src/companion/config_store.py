"""Persistent appliance configuration stored in data_dir/config.json.

ConfigStore is the single owner of the config file. Pass one instance to
both the portal router and the services router so they share the same file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel


class PortalConfig(BaseModel):
    """Appliance configuration persisted across restarts."""

    device_name: str = "PartyBox"
    spotify_connect_name: str = "PartyBox Companion"
    spotify_bitrate: Literal[96, 160, 320] = 320


class ConfigStore:
    """Read/write PortalConfig from a JSON file on disk."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> PortalConfig:
        if self._path.exists():
            return PortalConfig.model_validate(json.loads(self._path.read_text()))
        return PortalConfig()

    def write(self, cfg: PortalConfig) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(cfg.model_dump_json(indent=2))
