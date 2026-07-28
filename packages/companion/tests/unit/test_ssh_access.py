"""Unit tests for SshAccessService and its validation helpers (ADR-043)."""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from companion.services import ssh_access
from companion.services.ssh_access import (
    GithubImportError,
    InvalidKeyError,
    SshAccessService,
    _atomic_write,
    fetch_github_keys,
    validate_authorized_keys_block,
    validate_github_username,
)

_GOOD_KEY = "ssh-ed25519 " + base64.b64encode(b"A" * 48).decode() + " user@example.com"
_GOOD_KEY_2 = "ssh-rsa " + base64.b64encode(b"B" * 96).decode()


# ---------------------------------------------------------------------------
# validate_authorized_keys_block
# ---------------------------------------------------------------------------


def test_validate_accepts_single_valid_key() -> None:
    assert validate_authorized_keys_block(_GOOD_KEY) == [_GOOD_KEY]


def test_validate_accepts_multiple_lines() -> None:
    block = f"{_GOOD_KEY}\n{_GOOD_KEY_2}\n"
    assert validate_authorized_keys_block(block) == [_GOOD_KEY, _GOOD_KEY_2]


def test_validate_strips_blank_lines() -> None:
    block = f"\n\n{_GOOD_KEY}\n\n"
    assert validate_authorized_keys_block(block) == [_GOOD_KEY]


def test_validate_rejects_empty_block() -> None:
    with pytest.raises(InvalidKeyError, match="no public key"):
        validate_authorized_keys_block("   \n  \n")


def test_validate_rejects_unrecognized_type() -> None:
    with pytest.raises(InvalidKeyError, match="not a recognized"):
        validate_authorized_keys_block("dsa-key AAAA==")


def test_validate_rejects_options_prefix_injection() -> None:
    """The regex is anchored at the key type so an options prefix (e.g.
    forcing a command) can never sneak in disguised as a key line."""
    malicious = f'command="rm -rf /",no-pty {_GOOD_KEY}'
    with pytest.raises(InvalidKeyError, match="not a recognized"):
        validate_authorized_keys_block(malicious)


def test_validate_rejects_invalid_base64() -> None:
    # "QQ" matches the base64-alphabet character class the regex requires
    # (so it reaches the decode step) but is not validly padded base64.
    with pytest.raises(InvalidKeyError, match="base64"):
        validate_authorized_keys_block("ssh-ed25519 QQ")


def test_validate_rejects_too_short_key_body() -> None:
    short = "ssh-ed25519 " + base64.b64encode(b"short").decode()
    with pytest.raises(InvalidKeyError, match="too short"):
        validate_authorized_keys_block(short)


def test_validate_rejects_control_characters() -> None:
    with pytest.raises(InvalidKeyError, match="malformed"):
        validate_authorized_keys_block(_GOOD_KEY + "\x01evil")


def test_validate_rejects_too_many_keys() -> None:
    block = "\n".join([_GOOD_KEY] * 21)
    with pytest.raises(InvalidKeyError, match="too many"):
        validate_authorized_keys_block(block)


# ---------------------------------------------------------------------------
# validate_github_username
# ---------------------------------------------------------------------------


def test_github_username_accepts_valid() -> None:
    assert validate_github_username("octocat") == "octocat"
    assert validate_github_username(" some-user123 ") == "some-user123"


@pytest.mark.parametrize(
    "bad",
    ["-leadinghyphen", "trailinghyphen-", "has--double", "has space", "", "a" * 40],
)
def test_github_username_rejects_invalid(bad: str) -> None:
    with pytest.raises(GithubImportError):
        validate_github_username(bad)


# ---------------------------------------------------------------------------
# fetch_github_keys
# ---------------------------------------------------------------------------


def _fake_httpx_client(status_code: int, text: str) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.text = text

    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


async def test_fetch_github_keys_success() -> None:
    client = _fake_httpx_client(200, f"{_GOOD_KEY}\n{_GOOD_KEY_2}\n")
    with patch.object(ssh_access.httpx, "AsyncClient", return_value=client):
        keys = await fetch_github_keys("octocat")
    assert keys == [_GOOD_KEY, _GOOD_KEY_2]


async def test_fetch_github_keys_404_raises() -> None:
    client = _fake_httpx_client(404, "Not Found")
    with patch.object(ssh_access.httpx, "AsyncClient", return_value=client):
        with pytest.raises(GithubImportError, match="no GitHub user"):
            await fetch_github_keys("doesnotexist")


async def test_fetch_github_keys_429_raises_rate_limit_error() -> None:
    client = _fake_httpx_client(429, "rate limited")
    with patch.object(ssh_access.httpx, "AsyncClient", return_value=client):
        with pytest.raises(GithubImportError, match="rate-limited"):
            await fetch_github_keys("octocat")


async def test_fetch_github_keys_500_raises_server_error() -> None:
    client = _fake_httpx_client(503, "Service Unavailable")
    with patch.object(ssh_access.httpx, "AsyncClient", return_value=client):
        with pytest.raises(GithubImportError, match="server error"):
            await fetch_github_keys("octocat")


async def test_fetch_github_keys_other_status_raises_generic_error() -> None:
    client = _fake_httpx_client(301, "Moved")
    with patch.object(ssh_access.httpx, "AsyncClient", return_value=client):
        with pytest.raises(GithubImportError, match="unexpected status 301"):
            await fetch_github_keys("octocat")


async def test_fetch_github_keys_empty_body_raises() -> None:
    client = _fake_httpx_client(200, "")
    with patch.object(ssh_access.httpx, "AsyncClient", return_value=client):
        with pytest.raises(GithubImportError, match="no usable keys"):
            await fetch_github_keys("octocat")


async def test_fetch_github_keys_network_error_raises() -> None:
    client = MagicMock()
    client.get = AsyncMock(side_effect=httpx.ConnectTimeout("timed out"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with patch.object(ssh_access.httpx, "AsyncClient", return_value=client):
        with pytest.raises(GithubImportError, match=r"could not reach github\.com"):
            await fetch_github_keys("octocat")


async def test_fetch_github_keys_rejects_bad_username_before_network_call() -> None:
    with pytest.raises(GithubImportError):
        await fetch_github_keys("-bad")


# ---------------------------------------------------------------------------
# _atomic_write
# ---------------------------------------------------------------------------


def test_atomic_write_creates_file_with_content(tmp_path: Path) -> None:
    target = tmp_path / "f"
    _atomic_write(target, "hello\n")
    assert target.read_text() == "hello\n"


def test_atomic_write_overwrites_existing_content(tmp_path: Path) -> None:
    target = tmp_path / "f"
    target.write_text("old content that is longer than the new one\n")
    _atomic_write(target, "new\n")
    assert target.read_text() == "new\n"


def test_atomic_write_leaves_no_tmp_file_behind(tmp_path: Path) -> None:
    target = tmp_path / "f"
    _atomic_write(target, "hello\n")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f"]


# ---------------------------------------------------------------------------
# SshAccessService
# ---------------------------------------------------------------------------


def test_status_defaults_when_no_files_exist(tmp_path: Path) -> None:
    svc = SshAccessService(tmp_path)
    status = svc.status()
    assert status.enabled is False
    assert status.has_key is False
    assert status.authorized_keys == []
    assert status.applied_at is None
    assert status.error is None
    # Nothing has ever been requested, so there's nothing pending to confirm.
    assert status.confirmed is True


async def test_status_reads_back_applied_keys(tmp_path: Path) -> None:
    """The Portal round-trips the key field from this -- GET /api/v1/ssh/status
    must return the actual key content, not just a has_key boolean."""
    svc = SshAccessService(tmp_path)
    with patch.object(ssh_access.systemd1_dbus, "start_unit", new=AsyncMock()):
        await svc.apply(authorized_keys=[_GOOD_KEY, _GOOD_KEY_2])
    assert svc.status().authorized_keys == [_GOOD_KEY, _GOOD_KEY_2]


async def test_apply_writes_desired_state_and_triggers_unit(tmp_path: Path) -> None:
    svc = SshAccessService(tmp_path)
    with patch.object(ssh_access.systemd1_dbus, "start_unit", new=AsyncMock()) as start_unit:
        await svc.apply(authorized_keys=[_GOOD_KEY])

    start_unit.assert_awaited_once_with("companion-ssh-apply.service")
    assert (tmp_path / "ssh_enabled").read_text().strip() == "true"
    assert (tmp_path / "ssh_authorized_key").read_text() == _GOOD_KEY + "\n"


async def test_apply_empty_list_with_no_prior_key_stays_disabled(tmp_path: Path) -> None:
    svc = SshAccessService(tmp_path)
    with patch.object(ssh_access.systemd1_dbus, "start_unit", new=AsyncMock()):
        await svc.apply(authorized_keys=[])
    assert (tmp_path / "ssh_enabled").read_text().strip() == "false"


async def test_apply_empty_list_clears_key_and_disables(tmp_path: Path) -> None:
    svc = SshAccessService(tmp_path)
    with patch.object(ssh_access.systemd1_dbus, "start_unit", new=AsyncMock()):
        await svc.apply(authorized_keys=[_GOOD_KEY])
        await svc.apply(authorized_keys=[])
    assert (tmp_path / "ssh_authorized_key").read_text() == ""
    assert (tmp_path / "ssh_enabled").read_text().strip() == "false"


async def test_apply_none_leaves_previous_key_and_enabled_state_untouched(tmp_path: Path) -> None:
    svc = SshAccessService(tmp_path)
    with patch.object(ssh_access.systemd1_dbus, "start_unit", new=AsyncMock()):
        await svc.apply(authorized_keys=[_GOOD_KEY])
        await svc.apply(authorized_keys=None)
    assert (tmp_path / "ssh_authorized_key").read_text() == _GOOD_KEY + "\n"
    assert (tmp_path / "ssh_enabled").read_text().strip() == "true"


def test_status_reads_status_file_over_desired_state(tmp_path: Path) -> None:
    (tmp_path / "ssh_enabled").write_text("true\n")
    (tmp_path / "ssh_authorized_key").write_text(_GOOD_KEY + "\n")
    (tmp_path / "ssh_status.json").write_text(
        json.dumps(
            {
                "enabled": False,
                "has_key": True,
                "applied_at": "2026-07-23T00:00:00Z",
                "error": "no public key configured",
            }
        )
    )
    status = SshAccessService(tmp_path).status()
    assert status.enabled is False
    assert status.has_key is True
    assert status.applied_at == "2026-07-23T00:00:00Z"
    assert status.error == "no public key configured"


def test_status_falls_back_when_status_file_corrupt(tmp_path: Path) -> None:
    (tmp_path / "ssh_enabled").write_text("true\n")
    (tmp_path / "ssh_status.json").write_text("{not json")
    status = SshAccessService(tmp_path).status()
    assert status.enabled is True
    assert status.applied_at is None
    # An unreadable status file can't confirm anything -- fall back to
    # reporting the desired-state files' content as unconfirmed, not fact.
    assert status.confirmed is False


# ---------------------------------------------------------------------------
# SshAccessService.status() -- confirmed (ADR-043's status-staleness gap)
# ---------------------------------------------------------------------------


async def test_status_unconfirmed_when_status_file_never_written(tmp_path: Path) -> None:
    """Simulates a D-Bus trigger that errors/times out before the root unit
    ever reports back: desired state lands on disk, but ssh_status.json is
    never written at all. status() must not present has_key as confirmed
    fact just because the desired-state file says so."""
    svc = SshAccessService(tmp_path)
    with patch.object(ssh_access.systemd1_dbus, "start_unit", new=AsyncMock()):
        await svc.apply(authorized_keys=[_GOOD_KEY])
    status = svc.status()
    assert status.has_key is True
    assert status.confirmed is False


def test_status_unconfirmed_when_status_file_older_than_desired_state(tmp_path: Path) -> None:
    """Simulates a stale ssh_status.json surviving a reboot: the Pi lost
    power mid-run of the root apply script, so its EXIT trap (which would
    normally rewrite ssh_status.json even on failure) never fired, leaving
    behind a status file that predates the desired state it should describe."""
    enabled_path = tmp_path / "ssh_enabled"
    key_path = tmp_path / "ssh_authorized_key"
    status_path = tmp_path / "ssh_status.json"

    old_time = time.time() - 100
    status_path.write_text(
        json.dumps(
            {
                "enabled": False,
                "has_key": False,
                "applied_at": "2026-07-20T00:00:00Z",
                "error": None,
            }
        )
    )
    os.utime(status_path, (old_time, old_time))

    # A later apply() wrote new desired state, but the root unit never got
    # to (re)write ssh_status.json to match -- e.g. the appliance rebooted
    # mid-run.
    enabled_path.write_text("true\n")
    key_path.write_text(_GOOD_KEY + "\n")

    status = SshAccessService(tmp_path).status()
    assert status.confirmed is False
    # Behavior for enabled/has_key is unchanged (still the stale status
    # file's content) -- only `confirmed` signals it's unverified.
    assert status.enabled is False


def test_status_confirmed_when_status_file_newer_than_desired_state(tmp_path: Path) -> None:
    enabled_path = tmp_path / "ssh_enabled"
    key_path = tmp_path / "ssh_authorized_key"
    status_path = tmp_path / "ssh_status.json"

    old_time = time.time() - 100
    enabled_path.write_text("true\n")
    key_path.write_text(_GOOD_KEY + "\n")
    os.utime(enabled_path, (old_time, old_time))
    os.utime(key_path, (old_time, old_time))

    status_path.write_text(
        json.dumps(
            {
                "enabled": True,
                "has_key": True,
                "applied_at": "2026-07-28T00:00:00Z",
                "error": None,
            }
        )
    )

    status = SshAccessService(tmp_path).status()
    assert status.confirmed is True
