"""API key authentication dependency factory."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Header, HTTPException

from partyboxd.config import Settings


def make_auth_dependency(settings: Settings) -> Callable[..., Awaitable[None]]:
    """Return an async FastAPI dependency that enforces API key authentication.

    When ``settings.api.api_key`` is ``None`` all requests pass through
    without credential checks — useful for local-only deployments.

    Clients must supply the key in the ``X-Api-Key`` request header.
    """
    expected = settings.api.api_key

    async def check_api_key(
        x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    ) -> None:
        if expected is not None and x_api_key != expected:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "unauthorized",
                    "message": "Valid X-Api-Key header required.",
                },
            )

    return check_api_key
