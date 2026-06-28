"""Companion appliance configuration.

Settings are read from environment variables (prefix ``COMPANION_``)::

    COMPANION_HOST=0.0.0.0
    COMPANION_PORT=80
    COMPANION_DATA_DIR=/var/lib/companion
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class CompanionSettings(BaseSettings):
    """Top-level companion appliance settings.

    The companion is responsible for running the HTTP server and the Portal.
    Speaker / daemon settings (PARTYBOXD_*) are kept separate and read
    independently by :mod:`partyboxd.config`.

    Override any value with an environment variable::

        COMPANION_PORT=80 partybox-companion
    """

    model_config = SettingsConfigDict(
        env_prefix="COMPANION_",
        env_nested_delimiter="__",
    )

    host: str = "0.0.0.0"  # noqa: S104 — appliance must be reachable on the local network
    port: int = Field(default=8080, ge=1, le=65535)
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".local" / "share" / "companion")
