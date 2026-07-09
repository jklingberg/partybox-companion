"""Tests for GET /api/v1/health/details — per-task supervisor health."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock

from companion.config import SpotifySettings
from companion.config_store import ConfigStore
from companion.services.router import make_services_router
from companion.services.spotify import SpotifyStatus
from companion.supervisor import RestartPolicy, Supervisor
from httpx import ASGITransport, AsyncClient
from partyboxd.api import create_app as create_daemon_app
from partyboxd.api.auth import make_auth_dependency
from partyboxd.config import ApiSettings
from partyboxd.config import Settings as DaemonSettings
from partyboxd.device.manager import StatusSnapshot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager() -> MagicMock:
    manager = MagicMock()
    type(manager).snapshot = PropertyMock(
        return_value=StatusSnapshot(connected=False, address=None, firmware=None, battery=None)
    )
    return manager


def _make_spotify() -> MagicMock:
    spotify = MagicMock()
    type(spotify).status = PropertyMock(
        return_value=SpotifyStatus(running=False, active=False, device_name="PartyBox")
    )
    spotify.settings = SpotifySettings()
    return spotify


def _make_client(
    supervisor: Supervisor | None,
    tmp_path: Path,
    *,
    daemon_settings: DaemonSettings | None = None,
    with_auth: bool = False,
) -> AsyncClient:
    store = ConfigStore(tmp_path / "config.json")
    settings = daemon_settings or DaemonSettings()
    app = create_daemon_app(_make_manager(), settings)
    app.include_router(
        make_services_router(
            _make_spotify(),
            store,
            supervisor=supervisor,
            auth=make_auth_dependency(settings) if with_auth else None,
        )
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_no_supervisor_returns_empty_task_list(tmp_path: Path) -> None:
    async with _make_client(None, tmp_path) as client:
        r = await client.get("/api/v1/health/details")
    assert r.status_code == 200
    assert r.json() == {"tasks": []}


async def test_healthy_task_reports_running_state(tmp_path: Path) -> None:
    started = asyncio.Event()

    async def forever() -> None:
        started.set()
        await asyncio.get_running_loop().create_future()

    supervisor = Supervisor()
    supervisor.register("device-manager", forever)
    task = asyncio.create_task(supervisor.run())
    await asyncio.wait_for(started.wait(), timeout=2.0)

    async with _make_client(supervisor, tmp_path) as client:
        r = await client.get("/api/v1/health/details")

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert r.status_code == 200
    body = r.json()
    assert body == {
        "tasks": [
            {
                "name": "device-manager",
                "state": "running",
                "last_exception": None,
                "total_failures": 0,
            }
        ]
    }


async def test_crashed_task_reports_formatted_exception(tmp_path: Path) -> None:
    stable = asyncio.Event()
    attempts = 0

    async def crashes_once() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("boom")
        stable.set()
        await asyncio.get_running_loop().create_future()

    supervisor = Supervisor()
    supervisor.register("crasher", crashes_once, policy=RestartPolicy(initial_delay=0.0))
    task = asyncio.create_task(supervisor.run())
    await asyncio.wait_for(stable.wait(), timeout=2.0)

    async with _make_client(supervisor, tmp_path) as client:
        r = await client.get("/api/v1/health/details")

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert r.status_code == 200
    body = r.json()
    assert body == {
        "tasks": [
            {
                "name": "crasher",
                "state": "running",
                "last_exception": "RuntimeError: boom",
                "total_failures": 1,
            }
        ]
    }


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


async def test_requires_auth_when_configured(tmp_path: Path) -> None:
    settings = DaemonSettings(api=ApiSettings(api_key="secret"))
    async with _make_client(
        Supervisor(), tmp_path, daemon_settings=settings, with_auth=True
    ) as client:
        r = await client.get("/api/v1/health/details")
    assert r.status_code == 401


async def test_accepts_valid_api_key(tmp_path: Path) -> None:
    settings = DaemonSettings(api=ApiSettings(api_key="secret"))
    async with _make_client(
        Supervisor(), tmp_path, daemon_settings=settings, with_auth=True
    ) as client:
        r = await client.get("/api/v1/health/details", headers={"X-Api-Key": "secret"})
    assert r.status_code == 200


async def test_no_auth_dependency_means_no_key_required(tmp_path: Path) -> None:
    """When the caller doesn't pass `auth=`, the route is unauthenticated (as in tests above)."""
    async with _make_client(Supervisor(), tmp_path, with_auth=False) as client:
        r = await client.get("/api/v1/health/details")
    assert r.status_code == 200
