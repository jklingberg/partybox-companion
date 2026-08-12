"""Companion appliance configuration.

Settings are read from environment variables (prefix ``COMPANION_``)::

    COMPANION_HOST=0.0.0.0
    COMPANION_PORT=80
    COMPANION_DATA_DIR=/var/lib/companion
    COMPANION_RUNTIME_DIR=/run/companion
    COMPANION_SPOTIFY__CONNECT_NAME=Living Room
    COMPANION_SPOTIFY__BITRATE=320
    COMPANION_AUDIO__SINK_ADDRESS=50:1B:6A:14:FD:1D
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AudioSettings(BaseModel):
    """Settings for Bluetooth A2DP audio sink management.

    Override with environment variables::

        COMPANION_AUDIO__SINK_ADDRESS="50:1B:6A:14:FD:1D"
        COMPANION_AUDIO__AMP_BASELINE=52

    Set ``sink_address`` to the Bluetooth Classic (public) MAC address of the
    speaker. When set, the daemon establishes and maintains the A2DP connection
    so librespot always has an audio sink. When unset, A2DP management is
    disabled and the connection must be established externally.
    """

    sink_address: str | None = None

    #: Lowest speaker-amplifier level (AVRCP absolute volume, 0-127) the
    #: appliance will accept on a fresh A2DP connect. Raised to this if the
    #: speaker reports lower; never lowered — see
    #: ``companion.services.avrcp_volume``, which explains the floor semantics
    #: and why this cannot be derived from the digital gain stages.
    #:
    #: Calibrated by ear on hardware (2026-08-12) against the final taper
    #: (cubic, range 30), and validated at BOTH slider extremes — 1% and 100%
    #: were each judged right. That is a stronger check than the single-point
    #: judgements that preceded it, two of which pointed in opposite directions
    #: because the slider position was not held constant between them.
    #:
    #:      40   the INC-2 symptom (the speaker's own remembered level)
    #:      52   accepted across the whole slider span
    #:      72   too loud at every slider position
    #:     127   far too loud
    #:
    #: 52 is also the level the operator had independently arrived at earlier by
    #: turning the speaker's own knob, which corroborates it as a real
    #: preference rather than an artefact of one listening pass.
    #:
    #: It only has to be a sane floor, not anyone's maximum: the operator's
    #: decision was that the speaker's own knob is the way up when someone wants
    #: more. That also settles what happens below the baseline — the knob is for
    #: going louder, so a level under this one is drift to be corrected rather
    #: than an instruction to respect.
    amp_baseline: int = Field(default=52, ge=0, le=127)


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


class WifiSettings(BaseModel):
    """Settings for WiFi provisioning.

    Override with environment variables::

        COMPANION_WIFI__INTERFACE=wlan1
    """

    interface: str = "wlan0"


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
    # SpotifyService's librespot --onevent Unix socket lives here (the
    # --onevent *target* is a real console-script installed in the venv's
    # bin/, not a file under this dir — see spotify.py's module docstring and
    # issue #99, since on the appliance this is a noexec tmpfs). On the
    # appliance, the systemd unit overrides this to /run/companion
    # (RuntimeDirectory=companion — tmpfs, cleared on every restart, which is
    # what we want for ephemeral playback-state signalling).
    runtime_dir: Path = Field(default_factory=lambda: Path(tempfile.gettempdir()) / "companion")
    audio: AudioSettings = Field(default_factory=AudioSettings)
    spotify: SpotifySettings = Field(default_factory=SpotifySettings)
    wifi: WifiSettings = Field(default_factory=WifiSettings)
