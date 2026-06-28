"""Companion Portal router: config API and Portal HTML serving."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from companion.config import CompanionSettings
from companion.config_store import ConfigStore, PortalConfig

_STATIC_DIR = Path(__file__).parent / "static"


def make_portal_router(settings: CompanionSettings) -> tuple[APIRouter, ConfigStore]:
    """Return an APIRouter with companion config endpoints and the Portal.

    Also returns the ConfigStore so the caller can pass it to other routers
    that need config access (e.g. the services router for Spotify restart).

    Config endpoints are intentionally unauthenticated — they only hold
    non-sensitive appliance metadata. Speaker control endpoints in partyboxd
    carry the auth requirement.
    """
    router = APIRouter()
    store = ConfigStore(settings.data_dir / "config.json")

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

        Always returns **200**. Defaults are returned on first boot.
        """
        return store.read()

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
        """Persist the appliance configuration and return it.

        To apply changed Spotify settings without rebooting, call
        ``POST /api/v1/spotify/restart`` after this endpoint.
        """
        store.write(cfg)
        return cfg

    # ------------------------------------------------------------------
    # GET / — Portal HTML (catch-all, must come last)
    # ------------------------------------------------------------------

    @router.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def portal() -> str:
        """Serve the Companion Portal single-page application."""
        return (_STATIC_DIR / "index.html").read_text()

    return router, store
