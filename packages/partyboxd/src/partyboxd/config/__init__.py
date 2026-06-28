"""partyboxd configuration.

Settings are read from environment variables (prefix ``PARTYBOXD_``).
Nested settings use double-underscore as separator, e.g.::

    PARTYBOXD_SERVER__PORT=9000
    PARTYBOXD_SPEAKER__SCAN_TIMEOUT=12.0

All settings have reasonable defaults so the daemon works out of the box
with no configuration.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiSettings(BaseModel):
    """Settings for the HTTP API."""

    api_key: str | None = None


class SpeakerSettings(BaseModel):
    """Settings for the BLE speaker connection."""

    scan_timeout: float = Field(default=8.0, gt=0)
    reconnect_delay: float = Field(default=5.0, ge=0)


class ServerSettings(BaseModel):
    """Settings for the HTTP server."""

    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)


class Settings(BaseSettings):
    """Top-level daemon settings.

    Override any value with an environment variable::

        PARTYBOXD_SERVER__PORT=9000 partyboxd
    """

    model_config = SettingsConfigDict(
        env_prefix="PARTYBOXD_",
        env_nested_delimiter="__",
    )

    api: ApiSettings = Field(default_factory=ApiSettings)
    speaker: SpeakerSettings = Field(default_factory=SpeakerSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
