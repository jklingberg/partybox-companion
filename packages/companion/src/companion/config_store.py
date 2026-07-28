"""Persistent appliance configuration stored in data_dir/config.json.

ConfigStore is the single owner of the config file. Pass one instance to
both the portal router and the services router so they share the same file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)


class PortalConfig(BaseModel):
    """Appliance configuration persisted across restarts."""

    spotify_connect_name: str = "PartyBox"
    spotify_bitrate: Literal[96, 160, 320] = 320
    audio_sink_address: str | None = None


class PortalConfigPatch(BaseModel):
    """Partial update to :class:`PortalConfig` — every field optional.

    Used by ``PUT /api/v1/config`` so a caller that only wants to change
    ``spotify_connect_name`` never has to know (or resend) the current
    ``audio_sink_address`` — see RACE-01. Fields the caller didn't include in
    the request body are absent from :attr:`model_fields_set` and left
    untouched by :meth:`ConfigStore.update`; a field explicitly sent as
    ``null`` (e.g. to clear the paired speaker) is still applied, since it
    *is* present in ``model_fields_set``.
    """

    spotify_connect_name: str | None = None
    spotify_bitrate: Literal[96, 160, 320] | None = None
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
        # Guards read-modify-write sequences (see `update`) so the two
        # independent writers — Settings-save and PairingService — can't
        # interleave and silently drop each other's field (RACE-01). A plain
        # asyncio.Lock is enough: both writers run as coroutines on the same
        # event loop, so this only has to rule out interleaving across
        # `await` points, not real concurrency across threads/processes.
        self._lock = asyncio.Lock()

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
        """Atomically replace the config file with *cfg*.

        Writes to a temp file in the same directory, fsyncs it, then
        `os.replace`s it onto the real path — `os.replace` is atomic on the
        same filesystem, so a process killed mid-write (e.g. the
        idle-battery-shutdown power-off) can never observe a truncated or
        half-written `config.json` (DEBT-03). The temp file lives alongside
        the target rather than in a shared tmp dir so `os.replace` stays a
        same-filesystem rename rather than a cross-filesystem copy.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_name(f"{self._path.name}.tmp-{os.getpid()}")
        try:
            with tmp_path.open("w") as f:
                f.write(cfg.model_dump_json(indent=2))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._path)
        finally:
            tmp_path.unlink(missing_ok=True)

    async def update(self, mutate: Callable[[PortalConfig], PortalConfig]) -> PortalConfig:
        """Atomically read-modify-write the config under a lock.

        *mutate* receives the current on-disk config and returns the config
        to persist. Serializing every writer through this method (rather
        than each doing its own `read()` + `write()`) is what closes RACE-01:
        a Settings-save PUT and a concurrent pairing persist can no longer
        interleave such that one clobbers the other's field with a stale
        value — each `update()` call always mutates the freshest on-disk
        state, not a copy a caller cached earlier.
        """
        async with self._lock:
            cfg = self.read()
            new_cfg = mutate(cfg)
            self.write(new_cfg)
            return new_cfg

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
