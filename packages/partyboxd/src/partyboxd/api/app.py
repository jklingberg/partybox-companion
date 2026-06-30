"""FastAPI application factory for partyboxd."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI

from partyboxd.config import Settings
from partyboxd.device import DeviceManager

from .auth import make_auth_dependency
from .routes import make_router
from .ws import make_ws_router


def create_app(
    manager: DeviceManager,
    settings: Settings,
    audio_ready_fn: Callable[[], bool] | None = None,
) -> FastAPI:
    """Create and return the FastAPI application.

    The app holds no global state. The :class:`~partyboxd.device.DeviceManager`
    is the single source of truth; routes read from it on each request.

    API key authentication is controlled by ``settings.api.api_key``. When
    ``None`` (the default) all requests are accepted without credentials.
    """
    app = FastAPI(
        title="partyboxd",
        version="0.1.0-dev",
        description=(
            "Headless daemon exposing a stable REST API and WebSocket event stream "
            "for JBL PartyBox speakers."
        ),
        docs_url="/api/docs",
        redoc_url=None,
    )

    auth = make_auth_dependency(settings)
    app.include_router(make_router(manager, auth, audio_ready_fn=audio_ready_fn))
    app.include_router(make_ws_router(manager, settings))
    return app
