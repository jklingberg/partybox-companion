"""FastAPI application factory for partyboxd."""

from __future__ import annotations

from fastapi import FastAPI

from partyboxd.device import DeviceManager

from .routes import make_router


def create_app(manager: DeviceManager) -> FastAPI:
    """Create and return the FastAPI application.

    The app holds no global state. The :class:`~partyboxd.device.DeviceManager`
    is the single source of truth; routes read from it on each request.
    """
    app = FastAPI(title="partyboxd", version="0.1.0-dev", docs_url=None, redoc_url=None)
    app.include_router(make_router(manager))
    return app
