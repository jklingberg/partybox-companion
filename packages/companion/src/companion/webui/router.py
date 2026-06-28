"""Companion Portal router: config API endpoints and Portal HTML serving."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from companion.config import CompanionSettings

_STATIC_DIR = Path(__file__).parent / "static"


class PortalConfig(BaseModel):
    """Persistent companion configuration stored on disk."""

    device_name: str = "PartyBox"


def make_portal_router(settings: CompanionSettings) -> APIRouter:
    """Return an APIRouter with companion config endpoints and the Portal.

    Config endpoints are intentionally unauthenticated — they only hold
    non-sensitive appliance metadata (device name, first-boot flag). Speaker
    control endpoints in partyboxd carry the auth requirement.
    """
    router = APIRouter()
    config_file = settings.data_dir / "config.json"

    def _read() -> PortalConfig:
        if config_file.exists():
            return PortalConfig.model_validate(json.loads(config_file.read_text()))
        return PortalConfig()

    def _write(cfg: PortalConfig) -> None:
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(cfg.model_dump_json(indent=2))

    # ------------------------------------------------------------------
    # GET /api/v1/config — unauthenticated
    # ------------------------------------------------------------------

    @router.get(
        "/api/v1/config",
        response_model=PortalConfig,
        tags=["portal"],
        summary="Appliance configuration",
    )
    async def get_config() -> PortalConfig:
        """Return the current appliance configuration.

        Always returns **200**. Defaults are returned when no config file exists
        (e.g. first boot before the setup wizard has run).
        """
        return _read()

    # ------------------------------------------------------------------
    # PUT /api/v1/config — unauthenticated
    # ------------------------------------------------------------------

    @router.put(
        "/api/v1/config",
        response_model=PortalConfig,
        tags=["portal"],
        summary="Update appliance configuration",
    )
    async def put_config(cfg: PortalConfig) -> PortalConfig:
        """Persist the appliance configuration and return it."""
        _write(cfg)
        return cfg

    # ------------------------------------------------------------------
    # GET / — Portal HTML (catch-all, must come last)
    # ------------------------------------------------------------------

    @router.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def portal() -> str:
        """Serve the Companion Portal single-page application."""
        return (_STATIC_DIR / "index.html").read_text()

    return router
