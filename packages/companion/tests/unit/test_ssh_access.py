"""Unit tests for SshAccessService and its validation helpers (ADR-042)."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from companion.services import ssh_access
from companion.services.ssh_access import (
    GithubImportError,
    InvalidKeyError,
    SshAccessService,
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
# SshAccessService
# ---------------------------------------------------------------------------


def test_status_defaults_when_no_files_exist(tmp_path: Path) -> None:
    svc = SshAccessService(tmp_path)
    status = svc.status()
    assert status.enabled is False
    assert status.has_key is False
    assert status.applied_at is None
    assert status.error is None


async def test_apply_rejects_enabling_with_no_key(tmp_path: Path) -> None:
    svc = SshAccessService(tmp_path)
    with pytest.raises(ValueError, match="no public key"):
        await svc.apply(enabled=True, authorized_keys=None)


async def test_apply_writes_desired_state_and_triggers_unit(tmp_path: Path) -> None:
    svc = SshAccessService(tmp_path)
    with patch.object(ssh_access.systemd1_dbus, "start_unit", new=AsyncMock()) as start_unit:
        await svc.apply(enabled=True, authorized_keys=[_GOOD_KEY])

    start_unit.assert_awaited_once_with("companion-ssh-apply.service")
    assert (tmp_path / "ssh_enabled").read_text().strip() == "true"
    assert (tmp_path / "ssh_authorized_key").read_text() == _GOOD_KEY + "\n"


async def test_apply_disable_does_not_require_key(tmp_path: Path) -> None:
    svc = SshAccessService(tmp_path)
    with patch.object(ssh_access.systemd1_dbus, "start_unit", new=AsyncMock()):
        await svc.apply(enabled=False, authorized_keys=None)
    assert (tmp_path / "ssh_enabled").read_text().strip() == "false"


async def test_apply_empty_list_clears_key(tmp_path: Path) -> None:
    svc = SshAccessService(tmp_path)
    with patch.object(ssh_access.systemd1_dbus, "start_unit", new=AsyncMock()):
        await svc.apply(enabled=True, authorized_keys=[_GOOD_KEY])
        with pytest.raises(ValueError, match="no public key"):
            await svc.apply(enabled=True, authorized_keys=[])
    assert (tmp_path / "ssh_authorized_key").read_text() == ""


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
