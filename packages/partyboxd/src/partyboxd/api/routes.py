"""HTTP routes for partyboxd."""

from __future__ import annotations

import dataclasses

from fastapi import APIRouter
from pydantic import BaseModel

from partyboxd.device import DeviceManager


class StatusResponse(BaseModel):
    """Response body for GET /api/v1/status."""

    connected: bool
    address: str | None
    firmware: str | None
    battery: int | None


def make_router(manager: DeviceManager) -> APIRouter:
    """Return an APIRouter with all partyboxd routes bound to *manager*."""
    router = APIRouter(prefix="/api/v1")

    @router.get("/status", response_model=StatusResponse)
    async def get_status() -> StatusResponse:
        """Current daemon and speaker status."""
        return StatusResponse(**dataclasses.asdict(manager.snapshot))

    return router
