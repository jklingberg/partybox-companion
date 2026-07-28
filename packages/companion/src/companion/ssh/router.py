"""SSH access REST endpoints (ADR-043 / SEC-01)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from companion.services.ssh_access import (
    GithubImportError,
    InvalidKeyError,
    SshAccessService,
    fetch_github_keys,
    validate_authorized_keys_block,
)

log = logging.getLogger(__name__)


class SshStatusResponse(BaseModel):
    enabled: bool
    has_key: bool
    authorized_keys: list[str]
    applied_at: str | None = None
    error: str | None = None
    # See ADR-043's Consequences section / SshStatus.confirmed. False means
    # enabled/has_key are not known to reflect what the root apply unit
    # actually did -- e.g. the D-Bus trigger below timed out or errored, or
    # (after a reboot) a stale ssh_status.json survived a hard power loss
    # mid-apply.
    confirmed: bool = True


class SshGithubImportRequest(BaseModel):
    username: str


class SshGithubImportResponse(BaseModel):
    keys: list[str]


class SshSettingsRequest(BaseModel):
    authorized_keys: list[str] | None = Field(
        default=None,
        description=(
            "Omit this field (or send it as `null`) to leave any previously "
            "configured key(s) — and therefore the enabled state — "
            "untouched. Send an empty list `[]` to clear them and disable "
            "SSH. Send a non-empty list to replace them and enable SSH. "
            "These three cases are distinct — an empty list is NOT the same "
            "as omitting the field, so don't default a form field to `[]` "
            "unless you actually mean 'clear the key(s) and disable SSH'. "
            "Each entry is one authorized_keys-formatted line (as returned "
            "by the github-import endpoint, or pasted directly by the "
            "user). There is no separate enable/disable flag: whether SSH "
            "ends up on is derived entirely from whether a key ends up "
            "configured."
        ),
    )


def make_ssh_router(
    ssh: SshAccessService,
    auth: Callable[..., Awaitable[None]] | None = None,
) -> APIRouter:
    """Return an APIRouter with SSH access endpoints.

    Every route here requires authentication when an API key is configured
    (the same dependency ``PUT /api/v1/config`` uses) — unlike WiFi's
    provisioning-state exemption (SEC-02's carve-out only applies to getting
    a fresh appliance *onto* the network), there is no bootstrapping reason
    to ever expose these unauthenticated: SSH is only reachable through the
    Portal once the appliance is already on its home network, and this
    endpoint can grant a persistent remote shell — a strictly higher-value
    target than anything else the config endpoints already gate.
    """
    router = APIRouter(
        prefix="/api/v1/ssh",
        tags=["ssh"],
        dependencies=[Depends(auth)] if auth is not None else [],
    )

    @router.get(
        "/status",
        response_model=SshStatusResponse,
        summary="SSH access state",
    )
    async def get_ssh_status() -> SshStatusResponse:
        """Current SSH enable/key state, as last applied by the root helper unit."""
        s = ssh.status()
        return SshStatusResponse(
            enabled=s.enabled,
            has_key=s.has_key,
            authorized_keys=s.authorized_keys,
            applied_at=s.applied_at,
            error=s.error,
            confirmed=s.confirmed,
        )

    @router.post(
        "/github-import",
        response_model=SshGithubImportResponse,
        summary="Preview a GitHub user's public SSH keys",
    )
    async def post_github_import(body: SshGithubImportRequest) -> SshGithubImportResponse:
        """Fetch and validate ``github.com/<username>.keys``.

        Preview only — does **not** enable SSH or install anything. Call
        ``PUT /api/v1/ssh/settings`` with the returned ``keys`` to apply
        them, so the user can see what they're about to install (and which
        GitHub account it came from) before it takes effect.
        """
        try:
            keys = await fetch_github_keys(body.username)
        except GithubImportError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return SshGithubImportResponse(keys=keys)

    @router.put(
        "/settings",
        response_model=SshStatusResponse,
        summary="Set the authorized key(s) — SSH follows automatically",
    )
    async def put_ssh_settings(body: SshSettingsRequest) -> SshStatusResponse:
        """Apply SSH settings.

        Waits for ``companion-ssh-apply.service`` to actually finish applying
        the change (see ``SshAccessService.apply`` / ``systemd1_dbus.
        start_unit``) before responding, so the returned status reflects the
        real post-apply outcome rather than a stale pre-change snapshot — the
        unit itself completes in well under a second in practice. ``GET
        /api/v1/ssh/status`` remains available to poll independently (e.g.
        after a page reload), the same pattern ``GET /api/v1/wifi/status``
        uses for the (genuinely long-running) WiFi connect flow.

        ``ssh.apply()`` can raise if the D-Bus trigger itself fails or times
        out (systemd down, polkit misconfigured) — the desired-state files
        it writes *before* triggering the unit are already on disk by then,
        so the failure is caught here rather than left to become an
        unhandled 500: the response still carries whatever ``ssh.status()``
        reports, with ``confirmed=False`` (see ``SshStatus.confirmed``)
        making it explicit that this outcome wasn't verified applied.
        """
        authorized_keys = body.authorized_keys
        # An empty list (clear the configured key(s)) and None (leave them
        # untouched) both skip validation deliberately — the validator
        # rejects an empty block outright, which is correct for "no key
        # supplied at all" but wrong for "explicitly clear the key(s)".
        if authorized_keys:
            try:
                authorized_keys = validate_authorized_keys_block("\n".join(authorized_keys))
            except InvalidKeyError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            await ssh.apply(authorized_keys=authorized_keys)
        except Exception:
            log.warning("ssh apply failed to confirm applied state", exc_info=True)

        s = ssh.status()
        return SshStatusResponse(
            enabled=s.enabled,
            has_key=s.has_key,
            authorized_keys=s.authorized_keys,
            applied_at=s.applied_at,
            error=s.error,
            confirmed=s.confirmed,
        )

    return router
