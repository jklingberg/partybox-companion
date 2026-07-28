"""SSH access management: Portal-driven enable/disable + key provisioning.

See ADR-043. ``companion.service`` runs with ``NoNewPrivileges=true`` and
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

import asyncio
import base64
import binascii
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from companion.services import systemd1_dbus

log = logging.getLogger(__name__)


def _atomic_write(path: Path, text: str) -> None:
    """Write *text* to *path* via a temp file + fsync + rename.

    ``companion-ssh-apply.sh`` (root) reads these same files concurrently
    with this process writing them; a plain ``write_text()`` truncates in
    place first, so a read racing the write could see an empty or partial
    file. ``os.replace`` is atomic on the same filesystem (both files live
    under the same ``data_dir``), so readers only ever see the old or the
    new content, never a partial write.
    """
    tmp_path = path.with_name(path.name + ".tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_path, path)


_APPLY_UNIT = "companion-ssh-apply.service"
_GITHUB_KEYS_URL = "https://github.com/{username}.keys"
_GITHUB_FETCH_TIMEOUT = 10.0

# Anchored at the key-type token rather than searching for it anywhere in the
# line — see the module docstring / ADR-043: OpenSSH's authorized_keys format
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

    Single attempt, no retry — this is a user-initiated Portal action (the
    "Import" button), not a background job, so a transient failure is
    surfaced immediately and the user can just click Import again rather
    than this call silently retrying behind their back.
    """
    username = validate_github_username(username)
    url = _GITHUB_KEYS_URL.format(username=username)
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        raise GithubImportError(f"could not reach github.com: {exc}") from exc

    if resp.status_code == 404:
        raise GithubImportError(f"no GitHub user {username!r} found")
    if resp.status_code == 429:
        raise GithubImportError("GitHub rate-limited this request — wait a minute and try again")
    if resp.status_code >= 500:
        raise GithubImportError(
            f"GitHub returned a server error ({resp.status_code}) — try again shortly"
        )
    if resp.status_code != 200:
        raise GithubImportError(
            f"GitHub returned unexpected status {resp.status_code} for {username!r}"
        )

    try:
        return validate_authorized_keys_block(resp.text)
    except InvalidKeyError as exc:
        raise GithubImportError(f"GitHub returned no usable keys ({exc})") from exc


@dataclass(frozen=True)
class SshStatus:
    enabled: bool
    has_key: bool
    authorized_keys: list[str]
    applied_at: str | None
    error: str | None
    # See ADR-043's Consequences section ("requested-but-unconfirmed"). False
    # means ``enabled``/``has_key`` are not known to reflect what
    # ``companion-ssh-apply.service`` (the root side) actually did -- either
    # it has never run at all, or it last ran *before* the current desired
    # state was written (a stale ``ssh_status.json`` that survived a reboot
    # mid-apply, or a D-Bus trigger that errored/timed out before the unit
    # reported back). Defaults to ``True`` so existing call sites that
    # construct ``SshStatus`` directly (mocks, tests) don't need updating.
    #
    # ``True`` covers two genuinely different situations -- don't read it as
    # "SSH is verified enabled", only as "no pending request is left
    # unconfirmed": (a) the root side's report is present and up to date
    # (see ``SshAccessService._is_confirmed``), or (b) neither desired-state
    # file has ever been written at all (a factory-fresh appliance) --
    # there's nothing pending to confirm, but that says nothing about
    # ``enabled``/``has_key`` themselves (which default to ``False`` in that
    # case). Always check ``enabled``/``has_key`` separately for the actual
    # state.
    confirmed: bool = True


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
        # Serializes apply() end-to-end (write desired state -> trigger the
        # root unit -> wait for it to finish, see systemd1_dbus.start_unit).
        # Without this, two overlapping calls could both write their desired
        # state before either triggers the unit, and the second trigger would
        # get merged by systemd into the first's already-running job instead
        # of starting a fresh run — silently dropping the second call's state.
        self._lock = asyncio.Lock()

    def _latest_desired_state_mtime(self) -> float | None:
        """mtime of the most-recently-written desired-state file, if any exist.

        ``None`` means neither file has ever been written -- a factory-fresh
        appliance that has never had SSH touched, which is trivially
        "confirmed" (there is no pending desired state to confirm).
        """
        mtimes = [p.stat().st_mtime for p in (self._enabled_file, self._key_file) if p.exists()]
        return max(mtimes) if mtimes else None

    @staticmethod
    def _is_confirmed(status_mtime: float | None, desired_state_mtime: float | None) -> bool:
        """The invariant behind ``SshStatus.confirmed`` (ADR-043), isolated
        so it has exactly one place to read and one place to change.

        **Invariant: ``ssh_status.json`` only counts as confirming the
        current desired state if it was written at or after the newest
        desired-state file.** ``SshAccessService.apply()`` always writes
        the desired-state files *first* and only *then* triggers the root
        unit, which writes ``ssh_status.json`` last (on success or, via its
        ``EXIT`` trap, on failure) -- so that ordering is guaranteed as long
        as the unit actually ran to the point of writing it. A status file
        older than the desired state it's supposed to describe (or missing
        entirely) means the run that should have produced a fresh one either
        never happened or never finished, regardless of what it says.

        *status_mtime* is ``None`` when ``ssh_status.json`` doesn't exist or
        couldn't be read at all. *desired_state_mtime* is ``None`` when
        neither desired-state file has ever been written (factory-fresh) --
        that case returns ``True`` because there is nothing pending to
        confirm, not because anything has been verified applied.
        """
        if desired_state_mtime is None:
            return True
        if status_mtime is None:
            return False
        return status_mtime >= desired_state_mtime

    def status(self) -> SshStatus:
        """Current SSH access state.

        Prefers the root apply unit's last-written ``ssh_status.json`` (the
        authoritative record of what was actually applied) and falls back to
        reading the desired-state files directly if that hasn't been written
        yet (e.g. a factory-fresh appliance that has never had SSH touched).

        ``authorized_keys`` always comes straight from ``self._key_file``
        rather than ``ssh_status.json`` — that file is companion's own
        desired-state file (written by this process, not the root unit), so
        it's readable without needing the root side to echo key content
        back. This is what lets the Portal show the actually-configured
        key(s) instead of a write-only field.

        ``confirmed`` (see ADR-043's Consequences section) distinguishes
        "known applied" from "requested, outcome unknown" -- see
        :meth:`_is_confirmed` for the exact invariant. This is a pure
        read-time check -- it doesn't need a boot-time reconciliation pass
        to catch the reboot case, since every call to :meth:`status`
        re-derives it from current file mtimes.

        When ``confirmed`` is ``False``, treat ``enabled``/``has_key``/
        ``applied_at``/``error`` as the *last confirmed report, if any* --
        not as a description of whatever request is currently pending.
        Concretely: if ``ssh_status.json`` exists but predates the current
        desired state (the stale-reboot case), ``error`` can be left over
        from a *previous*, different request rather than explaining why the
        current one hasn't confirmed -- it's the last thing the root side
        actually said, not a live diagnosis of the pending one.
        """
        enabled = self._enabled_file.exists() and self._enabled_file.read_text().strip() == "true"
        has_key = self._key_file.exists() and self._key_file.stat().st_size > 0
        authorized_keys = (
            [ln for ln in self._key_file.read_text().splitlines() if ln.strip()]
            if self._key_file.exists()
            else []
        )
        applied_at: str | None = None
        error: str | None = None
        status_mtime: float | None = None
        desired_state_mtime = self._latest_desired_state_mtime()

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
                status_mtime = self._status_file.stat().st_mtime

        confirmed = self._is_confirmed(status_mtime, desired_state_mtime)

        return SshStatus(
            enabled=enabled,
            has_key=has_key,
            authorized_keys=authorized_keys,
            applied_at=applied_at,
            error=error,
            confirmed=confirmed,
        )

    async def apply(self, *, authorized_keys: list[str] | None) -> None:
        """Persist desired state and trigger the root apply unit.

        *authorized_keys* of ``None`` leaves any previously configured
        key(s) untouched; an empty list clears them. There is no separate
        "enabled" input — whether ``ssh.service`` ends up running is derived
        entirely from whether a key ends up configured: a key means SSH is
        on, no key means it's off. This keeps the two states that used to be
        settable independently (a stored key with SSH switched off) from
        existing at all, since that combination has no legitimate use and
        was the one users kept landing in by accident (toggle left alone
        after adding a key).

        Waits for ``companion-ssh-apply.service`` to actually finish (see
        ``systemd1_dbus.start_unit``) before returning, so by the time this
        coroutine completes, :meth:`status` already reflects the outcome.
        """
        async with self._lock:
            self._data_dir.mkdir(parents=True, exist_ok=True)

            if authorized_keys is not None:
                prospective_has_key = bool(authorized_keys)
                body = ("\n".join(authorized_keys) + "\n") if authorized_keys else ""
                _atomic_write(self._key_file, body)
            else:
                prospective_has_key = self._key_file.exists() and self._key_file.stat().st_size > 0

            _atomic_write(self._enabled_file, "true\n" if prospective_has_key else "false\n")

            await systemd1_dbus.start_unit(_APPLY_UNIT)
