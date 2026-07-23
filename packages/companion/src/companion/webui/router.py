"""Companion Portal router: config API and Portal HTML serving."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from companion.config import CompanionSettings
from companion.config_store import ConfigStore, PortalConfig

_STATIC_DIR = Path(__file__).parent / "static"


def make_portal_router(
    settings: CompanionSettings,
    store: ConfigStore,
    auth: Callable[..., Awaitable[None]] | None = None,
) -> APIRouter:
    """Return an APIRouter with companion config endpoints and the Portal.

    *store* is the shared :class:`~companion.config_store.ConfigStore` instance
    owned by the caller. Passing it in (rather than constructing one here)
    ensures a single file handle is used across all routers.

    ``GET /api/v1/config`` stays unauthenticated — it only holds non-sensitive
    appliance metadata and the Portal must be able to read it with no API key
    configured. ``PUT /api/v1/config`` requires *auth* (the same API-key
    dependency partyboxd's private routes use) when *auth* is provided — it
    can rewrite the Spotify name/bitrate and remembered speaker address, and
    was previously reachable even when a key was configured (SEC-02). See
    ``docs/adr/041-host-origin-allowlist.md``.
    """
    router = APIRouter()

    # ------------------------------------------------------------------
    # /assets/* — fonts, favicon (self-hosted; no CDN dependency, since the
    # Portal must render fully on the appliance's own AP during provisioning)
    # ------------------------------------------------------------------

    router.mount(
        "/assets",
        StaticFiles(directory=_STATIC_DIR / "assets"),
        name="portal-assets",
    )

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
    # PUT /api/v1/config — authenticated when an API key is configured
    # ------------------------------------------------------------------

    @router.put(
        "/api/v1/config",
        response_model=PortalConfig,
        tags=["portal"],
        summary="Update appliance configuration",
        dependencies=[Depends(auth)] if auth is not None else [],
    )
    async def put_config(cfg: PortalConfig) -> PortalConfig:
        """Persist the appliance configuration and return it.

        To apply changed Spotify settings without rebooting, call
        ``POST /api/v1/spotify/restart`` after this endpoint.

        Requires authentication when an API key is configured (SEC-02).
        """
        store.write(cfg)
        return cfg

    # ------------------------------------------------------------------
    # GET / — Portal HTML (catch-all, must come last)
    # ------------------------------------------------------------------

    @router.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def portal() -> HTMLResponse:
        """Serve the Companion Portal single-page application.

        Sent with ``Cache-Control: no-cache`` so a freshly deployed appliance is
        picked up on the next page load — without it, browsers may serve a stale
        cached copy and miss new Portal features until a manual hard refresh.
        """
        html = (_STATIC_DIR / "index.html").read_text()
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    return router
