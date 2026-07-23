"""SSH access management: Portal-driven enable/disable + key provisioning.

See ADR-042. ``companion.service`` runs with ``NoNewPrivileges=true`` and
``ProtectSystem=strict`` and has no sudoers grant, so it cannot itself write
``/home/pi/.ssh/authorized_keys`` or toggle ``ssh.service`` — there is no
existing system D-Bus interface for "write this file for another user" the
way NetworkManager and logind cover the privileged operations elsewhere in
this codebase.

Instead, :class:`SshAccessService` writes its desired state to two plain
files under its own ``data_dir`` (already writable — no new permissions
needed) and asks systemd, over D-Bus, to start exactly one root-owned
oneshot unit that a narrow polkit rule authorizes it for:
``companion-ssh-apply.service`` (``image/config/companion-ssh-apply.sh``).
That unit does the actual privileged work and writes ``ssh_status.json`` for
this module to read back.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from companion.services import systemd1_dbus

log = logging.getLogger(__name__)

_APPLY_UNIT = "companion-ssh-apply.service"
_GITHUB_KEYS_URL = "https://github.com/{username}.keys"
_GITHUB_FETCH_TIMEOUT = 10.0

# Anchored at the key-type token rather than searching for it anywhere in the
# line — see the module docstring / ADR-042: OpenSSH's authorized_keys format
# allows an options prefix (command=...,no-pty ssh-ed25519 ...) before the key
# type, and matching anywhere would let a pasted or GitHub-fetched "key" smuggle
# in a forced command or other option. Only bare key lines are accepted.
_KEY_LINE_RE = re.compile(
    r"^(?P<type>ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)"
    r"|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com)"
    r" (?P<b64>[A-Za-z0-9+/]+=*)(?: (?P<comment>[ -~]*))?$"
)
# GitHub's actual username rule (alnum runs separated by single hyphens, no
# leading/trailing hyphen, no consecutive hyphens; length checked separately
# below) — validated before being interpolated into a URL.
_GITHUB_USERNAME_RE = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")
_GITHUB_USERNAME_MAX_LEN = 39
_MAX_KEYS = 20
_MAX_LINE_LEN = 8192
_MIN_KEY_BODY_BYTES = 32


class InvalidKeyError(ValueError):
    """A supplied authorized_keys line failed validation."""


class GithubImportError(ValueError):
    """The GitHub key-import lookup failed (bad username, no keys, network error)."""


def validate_authorized_keys_block(text: str) -> list[str]:
    """Validate a block of one or more authorized_keys lines.

    Returns the validated, trimmed lines in their original order. Raises
    :class:`InvalidKeyError` on the first problem found — nothing is ever
    applied partially; either the whole block is good or none of it is used.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise InvalidKeyError("no public key provided")
    if len(lines) > _MAX_KEYS:
        raise InvalidKeyError(f"too many keys (max {_MAX_KEYS})")

    validated = []
    for line in lines:
        if len(line) > _MAX_LINE_LEN or any(ord(c) < 0x20 for c in line):
            raise InvalidKeyError("malformed key line")
        match = _KEY_LINE_RE.match(line)
        if not match:
            raise InvalidKeyError(f"not a recognized SSH public key: {line[:40]!r}")
        try:
            decoded = base64.b64decode(match.group("b64"), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise InvalidKeyError("key body is not valid base64") from exc
        if len(decoded) < _MIN_KEY_BODY_BYTES:
            raise InvalidKeyError("key body too short to be a real key")
        validated.append(line)
    return validated


def validate_github_username(username: str) -> str:
    """Validate *username* against GitHub's own username rules.

    Raises :class:`GithubImportError` if invalid. Only a validated username
    is ever interpolated into the fetch URL.
    """
    username = username.strip()
    if len(username) > _GITHUB_USERNAME_MAX_LEN or not _GITHUB_USERNAME_RE.match(username):
        raise GithubImportError(f"{username!r} is not a valid GitHub username")
    return username


async def fetch_github_keys(username: str, *, timeout: float = _GITHUB_FETCH_TIMEOUT) -> list[str]:
    """Fetch and validate the given GitHub user's public SSH keys.

    Uses ``https://github.com/<user>.keys`` — the same public, unauthenticated
    endpoint Ubuntu's installer and cloud-init's ``ssh-import-id gh:<user>``
    use. GitHub publishes every account's registered SSH public keys there by
    design; no token or authentication is needed. This only fetches and
    validates — it does not apply anything.
    """
    username = validate_github_username(username)
    url = _GITHUB_KEYS_URL.format(username=username)
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        raise GithubImportError(f"could not reach github.com: {exc}") from exc

    if resp.status_code != 200:
        raise GithubImportError(f"no GitHub user {username!r} found")

    try:
        return validate_authorized_keys_block(resp.text)
    except InvalidKeyError as exc:
        raise GithubImportError(f"GitHub returned no usable keys ({exc})") from exc


@dataclass(frozen=True)
class SshStatus:
    enabled: bool
    has_key: bool
    applied_at: str | None
    error: str | None


class SshAccessService:
    """Owns the desired-state files ``companion-ssh-apply.service`` reads.

    Pass the shared ``data_dir`` companion already uses (its systemd
    ``StateDirectory``) — these files live alongside ``config.json``.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._enabled_file = data_dir / "ssh_enabled"
        self._key_file = data_dir / "ssh_authorized_key"
        self._status_file = data_dir / "ssh_status.json"

    def status(self) -> SshStatus:
        """Current SSH access state.

        Prefers the root apply unit's last-written ``ssh_status.json`` (the
        authoritative record of what was actually applied) and falls back to
        reading the desired-state files directly if that hasn't been written
        yet (e.g. a factory-fresh appliance that has never had SSH touched).
        """
        enabled = self._enabled_file.exists() and self._enabled_file.read_text().strip() == "true"
        has_key = self._key_file.exists() and self._key_file.stat().st_size > 0
        applied_at: str | None = None
        error: str | None = None

        if self._status_file.exists():
            try:
                data = json.loads(self._status_file.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("ssh status file unreadable: %s", exc)
            else:
                enabled = bool(data.get("enabled", enabled))
                has_key = bool(data.get("has_key", has_key))
                applied_at = data.get("applied_at")
                error = data.get("error")

        return SshStatus(enabled=enabled, has_key=has_key, applied_at=applied_at, error=error)

    async def apply(self, *, enabled: bool, authorized_keys: list[str] | None) -> None:
        """Persist desired state and trigger the root apply unit.

        *authorized_keys* of ``None`` leaves any previously configured
        key(s) untouched; an empty list clears them. Raises ``ValueError``
        if *enabled* is ``True`` but no key would end up configured —
        refusing to bring up ``ssh.service`` with nothing able to
        authenticate against it (``PasswordAuthentication`` stays ``no``
        regardless).
        """
        self._data_dir.mkdir(parents=True, exist_ok=True)

        if authorized_keys is not None:
            body = ("\n".join(authorized_keys) + "\n") if authorized_keys else ""
            self._key_file.write_text(body)

        has_key = self._key_file.exists() and self._key_file.stat().st_size > 0
        if enabled and not has_key:
            raise ValueError("cannot enable SSH with no public key configured")

        self._enabled_file.write_text("true\n" if enabled else "false\n")

        await systemd1_dbus.start_unit(_APPLY_UNIT)
