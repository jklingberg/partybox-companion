"""Companion appliance configuration.

Settings are read from environment variables (prefix ``COMPANION_``)::

    COMPANION_HOST=0.0.0.0
    COMPANION_PORT=80
    COMPANION_DATA_DIR=/var/lib/companion
    COMPANION_SPOTIFY__CONNECT_NAME=Living Room
    COMPANION_SPOTIFY__BITRATE=320
    COMPANION_AUDIO__SINK_ADDRESS=50:1B:6A:14:FD:1D
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AudioSettings(BaseModel):
    """Settings for Bluetooth A2DP audio sink management.

    Override with environment variables::

        COMPANION_AUDIO__SINK_ADDRESS="50:1B:6A:14:FD:1D"

    Set ``sink_address`` to the Bluetooth Classic (public) MAC address of the
    speaker. When set, the daemon establishes and maintains the A2DP connection
    so librespot always has an audio sink. When unset, A2DP management is
    disabled and the connection must be established externally.
    """

    sink_address: str | None = None


class SpotifySettings(BaseModel):
    """Settings for the Spotify Connect service (librespot).

    Override with environment variables::

        COMPANION_SPOTIFY__CONNECT_NAME="Living Room"
        COMPANION_SPOTIFY__BITRATE=160
        COMPANION_SPOTIFY__BACKEND=pulseaudio
    """

    connect_name: str = "PartyBox"
    bitrate: Literal[96, 160, 320] = 320
    backend: str | None = None


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
    audio: AudioSettings = Field(default_factory=AudioSettings)
    spotify: SpotifySettings = Field(default_factory=SpotifySettings)
