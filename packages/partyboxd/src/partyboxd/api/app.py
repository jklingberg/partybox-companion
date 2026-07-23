"""FastAPI application factory for partyboxd."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from fastapi import FastAPI
from starlette.types import Lifespan

from partyboxd.config import Settings
from partyboxd.device import DeviceManager

from .auth import make_auth_dependency
from .routes import make_router
from .security import HostOriginMiddleware
from .ws import EventSource, make_ws_router


def create_app(
    manager: DeviceManager,
    settings: Settings,
    audio_ready_fn: Callable[[], bool] | None = None,
    audio_focus_fn: Callable[[], str] | None = None,
    extra_event_sources: Sequence[EventSource] = (),
    lifespan: Lifespan[FastAPI] | None = None,
) -> FastAPI:
    """Create and return the FastAPI application.

    The app holds no global state. The :class:`~partyboxd.device.DeviceManager`
    is the single source of truth; routes read from it on each request.

    API key authentication is controlled by ``settings.api.api_key``. When
    ``None`` (the default) all requests are accepted without credentials.
    Independent of the API key, every request's Host (and, for mutating
    methods, Origin) header is validated by :class:`HostOriginMiddleware` —
    see ``docs/adr/041-host-origin-allowlist.md`` — which closes the
    CSRF/DNS-rebinding gap even with no key configured.

    *extra_event_sources* lets a layer above partyboxd (companion) fan
    additional events into the same WebSocket stream — see
    ``docs/adr/035-state-ownership-and-signal-pipeline.md``.

    *lifespan* is passed through to :class:`FastAPI`. Shutdown work that
    must complete before the process exits belongs there: uvicorn runs the
    lifespan inside ``serve()``, whereas code placed after ``serve()``
    never runs on a signal-initiated stop (uvicorn re-raises the captured
    signal the moment ``serve()`` returns).
    """
    app = FastAPI(
        lifespan=lifespan,
        title="partyboxd",
        version="0.1.0-dev",
        description=(
            "Headless daemon exposing a stable REST API and WebSocket event stream "
            "for JBL PartyBox speakers."
        ),
        docs_url="/api/docs",
        redoc_url=None,
    )
    app.add_middleware(HostOriginMiddleware)

    auth = make_auth_dependency(settings)
    app.include_router(
        make_router(manager, auth, audio_ready_fn=audio_ready_fn, audio_focus_fn=audio_focus_fn)
    )
    app.include_router(make_ws_router(manager, settings, extra_sources=extra_event_sources))
    return app
