"""Tests for the SSH access REST endpoints (ADR-042 / SEC-01)."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock

import pytest
from companion.services.ssh_access import SshAccessService, SshStatus
from companion.ssh.router import make_ssh_router
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from partyboxd.api.auth import make_auth_dependency
from partyboxd.config import ApiSettings
from partyboxd.config import Settings as DaemonSettings

# A syntactically real key -- PUT /api/v1/ssh/settings validates its input
# for real (unlike github-import's mocked fetch below), so a placeholder
# string like "ssh-ed25519 AAAA..." would be rejected as invalid base64.
_GOOD_KEY = "ssh-ed25519 " + base64.b64encode(b"A" * 48).decode()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(
    enabled: bool = False,
    has_key: bool = False,
    applied_at: str | None = None,
    error: str | None = None,
) -> MagicMock:
    svc = MagicMock(spec=SshAccessService)
    svc.status = MagicMock(
        return_value=SshStatus(enabled=enabled, has_key=has_key, applied_at=applied_at, error=error)
    )
    svc.apply = AsyncMock()
    return svc


def _make_client(
    svc: MagicMock,
    *,
    daemon_settings: DaemonSettings | None = None,
    with_auth: bool = False,
) -> AsyncClient:
    app = FastAPI()
    settings = daemon_settings or DaemonSettings()
    app.include_router(
        make_ssh_router(svc, auth=make_auth_dependency(settings) if with_auth else None)
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# GET /api/v1/ssh/status
# ---------------------------------------------------------------------------


async def test_status_reflects_service() -> None:
    svc = _make_service(enabled=True, has_key=True, applied_at="2026-07-23T00:00:00Z")
    async with _make_client(svc) as client:
        r = await client.get("/api/v1/ssh/status")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "enabled": True,
        "has_key": True,
        "applied_at": "2026-07-23T00:00:00Z",
        "error": None,
    }


async def test_status_requires_auth_when_configured() -> None:
    svc = _make_service()
    settings = DaemonSettings(api=ApiSettings(api_key="secret"))
    async with _make_client(svc, daemon_settings=settings, with_auth=True) as client:
        r = await client.get("/api/v1/ssh/status")
    assert r.status_code == 401


async def test_status_accepts_valid_api_key() -> None:
    svc = _make_service()
    settings = DaemonSettings(api=ApiSettings(api_key="secret"))
    async with _make_client(svc, daemon_settings=settings, with_auth=True) as client:
        r = await client.get("/api/v1/ssh/status", headers={"X-Api-Key": "secret"})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/v1/ssh/github-import
# ---------------------------------------------------------------------------


async def test_github_import_returns_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch(username: str) -> list[str]:
        assert username == "octocat"
        return ["ssh-ed25519 AAAA...", "ssh-rsa BBBB..."]

    import companion.ssh.router as router_module

    monkeypatch.setattr(router_module, "fetch_github_keys", fake_fetch)

    svc = _make_service()
    async with _make_client(svc) as client:
        r = await client.post("/api/v1/ssh/github-import", json={"username": "octocat"})
    assert r.status_code == 200
    assert r.json() == {"keys": ["ssh-ed25519 AAAA...", "ssh-rsa BBBB..."]}


async def test_github_import_failure_returns_400(monkeypatch: pytest.MonkeyPatch) -> None:
    from companion.services.ssh_access import GithubImportError

    async def fake_fetch(username: str) -> list[str]:
        raise GithubImportError("no GitHub user 'nobody' found")

    import companion.ssh.router as router_module

    monkeypatch.setattr(router_module, "fetch_github_keys", fake_fetch)

    svc = _make_service()
    async with _make_client(svc) as client:
        r = await client.post("/api/v1/ssh/github-import", json={"username": "nobody"})
    assert r.status_code == 400
    assert "no GitHub user" in r.json()["detail"]


async def test_github_import_does_not_touch_ssh_service() -> None:
    """Preview only -- apply() must never be called by this endpoint."""
    svc = _make_service()
    async with _make_client(svc) as client:
        await client.post("/api/v1/ssh/github-import", json={"username": "octocat"})
    svc.apply.assert_not_called()


# ---------------------------------------------------------------------------
# PUT /api/v1/ssh/settings
# ---------------------------------------------------------------------------


async def test_put_settings_applies_and_returns_status() -> None:
    svc = _make_service(enabled=True, has_key=True)
    async with _make_client(svc) as client:
        r = await client.put(
            "/api/v1/ssh/settings",
            json={"enabled": True, "authorized_keys": [_GOOD_KEY]},
        )
    assert r.status_code == 200
    svc.apply.assert_awaited_once_with(enabled=True, authorized_keys=[_GOOD_KEY])


async def test_put_settings_rejects_invalid_key() -> None:
    svc = _make_service()
    async with _make_client(svc) as client:
        r = await client.put(
            "/api/v1/ssh/settings",
            json={"enabled": True, "authorized_keys": ["not-a-key"]},
        )
    assert r.status_code == 400
    svc.apply.assert_not_called()


async def test_put_settings_empty_list_clears_keys_without_validation_error() -> None:
    svc = _make_service()
    async with _make_client(svc) as client:
        r = await client.put(
            "/api/v1/ssh/settings",
            json={"enabled": False, "authorized_keys": []},
        )
    assert r.status_code == 200
    svc.apply.assert_awaited_once_with(enabled=False, authorized_keys=[])


async def test_put_settings_none_leaves_keys_untouched() -> None:
    svc = _make_service()
    async with _make_client(svc) as client:
        r = await client.put("/api/v1/ssh/settings", json={"enabled": False})
    assert r.status_code == 200
    svc.apply.assert_awaited_once_with(enabled=False, authorized_keys=None)


async def test_put_settings_surfaces_service_value_error_as_400() -> None:
    svc = _make_service()
    svc.apply = AsyncMock(side_effect=ValueError("cannot enable SSH with no public key configured"))
    async with _make_client(svc) as client:
        r = await client.put("/api/v1/ssh/settings", json={"enabled": True})
    assert r.status_code == 400
    assert "no public key configured" in r.json()["detail"]


async def test_put_settings_requires_auth_when_configured() -> None:
    svc = _make_service()
    settings = DaemonSettings(api=ApiSettings(api_key="secret"))
    async with _make_client(svc, daemon_settings=settings, with_auth=True) as client:
        r = await client.put("/api/v1/ssh/settings", json={"enabled": False})
    assert r.status_code == 401
    svc.apply.assert_not_called()
