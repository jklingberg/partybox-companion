# ADR-043: SSH Hardening — No Shared Default Credentials

**Status:** Accepted

---

## Context

Every appliance image shipped with the same password-authenticated, sudo-capable
SSH account: `pi` / `raspberry` (`image/install.sh`, `echo "pi:raspberry" |
chpasswd`), alongside a `PasswordAuthentication yes` sshd drop-in that made the
image *more* exposed than stock Raspberry Pi OS Bookworm (which defaults to
`PasswordAuthentication prohibit-password`). [ADR-020](020-appliance-hardening.md)
flagged this at the time as an accepted development-phase trade-off that "must
be re-evaluated before v1.0" — v1.0 is now (SEC-01, issue #74).

On this product's actual use case — a speaker carried to parties and joined to
whatever ad-hoc or guest WiFi is available — a fixed, published default
password is effectively a root shell available to anyone who reads the
README, on every appliance in the field simultaneously.

Two constraints shaped the fix:

1. **`companion.service` cannot do this itself.** It runs with
   `NoNewPrivileges=true` and `ProtectSystem=strict` (`system/systemd/
   companion.service`) and has no sudoers grant — the same wall
   [ADR-028](028-audio-readiness-model.md) and [ADR-038](
   038-idle-battery-shutdown.md) already hit for unrelated privileged
   operations. Writing `/home/pi/.ssh/authorized_keys` and toggling
   `ssh.service` are both root-only operations with **no existing system
   D-Bus interface** to lean on — unlike NetworkManager ([ADR-021](
   021-network-provisioning.md)) or `logind` ([ADR-038](
   038-idle-battery-shutdown.md)), there is no stock daemon that exposes
   "write this file for another user" over D-Bus.
2. **The `pi` account itself must survive.** It still owns the PipeWire/
   WirePlumber user session Companion's audio pipeline depends on
   (`CLAUDE.md`'s "pi vs companion" section), and the physical UART serial
   console (`docs/adr/020-appliance-hardening.md`'s headless-boot section)
   is the only local-access path if the network is unreachable. Locking the
   account outright would remove that fallback entirely.

## Decision

### 1. SSH is disabled by default, everywhere — including dev images

`ssh.service` ships **disabled** on every image: production and
`devcontainer`/manually-installed images behave identically. There is no
build flag that restores the old password-auth behaviour. This removes the
attack surface entirely for the (likely common) user who never needs a
shell, and it means the one code path is exercised by everyone, including
this project's own contributors — the strongest possible test of it working.
The practical cost is that `CLAUDE.md`'s SSH deploy workflow (`sshpass -p
raspberry`) no longer works and has been rewritten (see "Consequences"
below): enable SSH and add a key via the Portal like any other user, once,
per device.

### 2. Enabling SSH and provisioning a key happens entirely in the Portal, post-WiFi

A new **SSH access** section in the Settings sheet lets the user:

- Paste one or more `authorized_keys`-formatted public key lines directly, **or**
- Enter a GitHub username; Companion fetches `https://github.com/<user>.keys`
  server-side (the same public, unauthenticated endpoint Ubuntu's installer
  and `ssh-import-id gh:<user>` / cloud-init use — GitHub publishes every
  account's public SSH keys there by design) and installs whatever keys come
  back, after validating each one.

This lives in the normal (post-`ProvisioningState.CONNECTED`) Portal, not the
AP-mode captive-portal setup flow — GitHub import needs outbound internet,
which the appliance doesn't have while it's still serving its own
provisioning AP. `GET /api/v1/ssh/status` and `PUT /api/v1/ssh/settings` both
require the same API-key auth as `PUT /api/v1/config` (SEC-02): this
endpoint can grant a persistent remote shell, which is a strictly higher-value
target than anything else `PUT /api/v1/config` already gates.

**There is no independent enable/disable toggle — whether `ssh.service` runs
is derived entirely from whether a key ends up configured** (`SshAccessService.
apply`: `enabled = bool(authorized_keys)`, always). The first shipped version
of this UI had a separate "Enable SSH" checkbox next to the key field, on the
reasoning that enabling with no key should be rejected outright as a
confusing dead end. In practice that gave the checkbox and the key field two
independent states that needed to agree, and real usage showed people adding
a key, clicking Apply, and never noticing the checkbox was a second step —
leaving a key configured but SSH still off, with no clear signal why. Tying
`enabled` to key presence removes that state entirely: adding a key turns SSH
on, clearing all keys turns it off. The dead-end case this was originally
guarding against (`PasswordAuthentication no` with nothing able to
authenticate) is now unreachable by construction rather than rejected at the
API boundary.

The key field itself went through the same fix a step further: it used to be
blank every time the Settings sheet opened (write-only, with a separate
"leave blank to keep existing keys" convention), so there was no way to see
what was actually configured, and a dedicated **Disable SSH** button existed
just to send `authorized_keys: []`. `GET /api/v1/ssh/status` now returns
`authorized_keys` (companion already has the plaintext on disk — it's the
file it writes as its own desired state before asking the root unit to
install it, and public keys aren't secret), and the Portal populates the
field from that on open. The field is a straightforward mirror of server
state both ways: Apply always replaces server state with exactly what's in
the field, so an emptied field plus Apply *is* disabling SSH — no separate
button needed.

**Key validation** (`companion.services.ssh_access.validate_authorized_keys_block`)
anchors its regex at the key-type token (`ssh-ed25519`, `ssh-rsa`,
`ecdsa-sha2-nistp*`, `sk-*@openssh.com`) rather than searching for it anywhere
in the line. OpenSSH's `authorized_keys` format allows an *options* prefix
before the key type (`command=...,no-pty ssh-ed25519 ...`); accepting that
would let a pasted or GitHub-fetched line smuggle in a forced command or
other option the user never intended. Only bare key lines are accepted — no
options syntax, full stop. The key body is also base64-decoded and
length-checked, and the whole block is capped at 20 keys / 8192 bytes per
line, to reject garbage before it ever reaches disk.

**SSH certificates are deliberately out of scope.** The recognized-type list
covers raw public keys only (`ssh-ed25519`, `ssh-rsa`, `ecdsa-sha2-nistp*`,
`sk-*@openssh.com`) — the `*-cert-v01@openssh.com` certificate types are not
accepted, so a pasted user certificate is rejected the same as any other
unrecognized type. Certificates imply a CA-trust model (anyone holding a key
the CA signed gets in, not just the holder of one specific key), which is a
different and heavier posture than this single-owner, paste-your-own-key
flow is built for. If a future use case needs it, that's a separate ADR, not
an addition to this validator.

### 3. A single narrow root oneshot unit does the actual privileged work

`companion` cannot write `/home/pi/.ssh/authorized_keys` or run `systemctl
enable/disable ssh.service` itself. Rather than build a general-purpose
privileged broker (rejected — see below), it writes its desired state to two
plain files it already owns (`data_dir/ssh_enabled`, `data_dir/
ssh_authorized_key` — no shell-interpreted format, just raw bytes copied
verbatim by the root side) and asks systemd, over D-Bus, to start exactly one
named unit:

```
companion (unprivileged, NoNewPrivileges)
    writes  ssh_enabled, ssh_authorized_key   (its own StateDirectory)
    calls   org.freedesktop.systemd1.Manager.StartUnit("companion-ssh-apply.service", "replace")
            (companion/services/systemd1_dbus.py — same dbus-fast pattern as login1_dbus.py)

companion-ssh-apply.service   (root, oneshot, Type=oneshot)
    reads   ssh_enabled, ssh_authorized_key
    writes  /home/pi/.ssh/authorized_keys  (mode 600, owner pi:pi)
    runs    systemctl enable --now ssh.service   /  disable --now ssh.service
    writes  ssh_status.json                (companion polls this for the Portal)
```

A polkit rule installed by `install.sh` grants `companion` exactly
`org.freedesktop.systemd1.manage-units`, **scoped to `action.lookup("unit")
== "companion-ssh-apply.service"`** — not the namespace, not even "any unit
companion happens to own." This mirrors the two precedents already
established (`51-companion-nm.rules`, namespace-scoped because NM's action-ID
surface is fragile across versions per ADR-021; `52-companion-logind.rules`,
scoped to two specific stable action IDs per ADR-038) — here the unit name
itself *is* the scope, which is narrower than either existing rule.

`start_unit()` waits for the triggered job's `JobRemoved` D-Bus signal before
returning, rather than returning as soon as `StartUnit` hands back a queued
job path. This isn't just "wait a bit longer for nicer UX": `mode="replace"`
only preempts a *queued* job, not one already executing, so a naive
fire-and-forget implementation would let a second `apply()` call arriving
while the previous run is still executing get **merged** by systemd into
that already-running job instead of starting a fresh one — silently
dropping whatever new desired state the second caller had just written to
disk, with no error surfaced anywhere. `SshAccessService.apply()` also holds
an `asyncio.Lock` across its whole write-then-trigger-then-wait sequence, so
two overlapping calls from Companion's own event loop can't even race each
other into writing interleaved desired-state before either one triggers the
unit. The oneshot unit's own work is a handful of file/systemctl operations
and completes in well under a second in practice, so waiting for it is a
short delay, not a long-poll — and it means `PUT /api/v1/ssh/settings`'s
response already reflects the real, post-apply status rather than a stale
pre-change one. `GET /api/v1/ssh/status` remains available to poll
independently, the same pattern `GET /api/v1/wifi/status` uses for the
(genuinely long-running) WiFi connect flow.

**Rejected: a full D-Bus broker service.** A previous privileged-recovery
discussion ([ADR-028](028-audio-readiness-model.md)'s deferred audio-recovery
broker) sketched a root-owned daemon exposing its own D-Bus interface
(`SetKey`, `SetEnabled`, ...) that Companion would call directly. For a
single, infrequent, non-interactive operation like this one, that is
strictly more code and more permanently-running root-owned surface than a
oneshot unit triggered on demand — the oneshot unit only exists (as a
process) for the fraction of a second it takes to apply a change, and its
entire behaviour is one auditable shell script, not a long-lived D-Bus
service with its own attack surface.

### 4. The `pi` account keeps a password, but a random per-device one — set on first real boot, not at image-build time

`install.sh` still creates the `pi` user, but no longer calls `chpasswd` with
a fixed string. Instead, a new oneshot unit, `partybox-firstboot.service`
(`ConditionPathExists=!/var/lib/companion/.firstboot-done`, ordered before
the getty units), runs once on the device's actual first boot — not during
the QEMU chroot image build, which happens once per *image* and would
otherwise give every device flashed from that image the same "random"
password, reproducing exactly the bug this ADR fixes. It:

- Generates a random password (`/dev/urandom`, unambiguous alphabet, 16
  chars) and sets it via `chpasswd`.
- Writes it into `/etc/issue`, which `getty`/`serial-getty` display before
  every login prompt — including the UART serial console
  (`docs/adr/020-appliance-hardening.md`'s headless-boot section) — so
  whoever has physical access to the device can read it there. It is never
  transmitted over the network by this mechanism.
- Touches the marker file so it never runs again.

This keeps the local-console recovery path ADR-020 relied on, while making
sure no two devices — and no two builds of the *same* image — ever share a
password. It is entirely independent of the SSH/Portal flow above: this
password is for the physical console only, never usable over SSH (which is
key-only whenever it's enabled at all).

**Caveat, stated plainly:** if the user changes this password, `/etc/issue`
is not updated to match — it will keep showing the original (now stale)
generated value. This is a cosmetic-only gap (an operator who changes a
password is, by definition, already aware of it); fixing it would mean
hooking password-change events, judged as more complexity than the gap
warrants.

### 5. Factory reset disables SSH and clears the key

[ADR-031](031-factory-reset-contract.md) requires new runtime state to
either be torn down by `POST /api/v1/factory-reset` or explicitly documented
as exempt — a fresh image ships with SSH disabled and no key, so the SSH
state this ADR adds isn't exempt. `post_factory_reset()` calls
`SshAccessService.apply(enabled=False, authorized_keys=[])` alongside the
existing bond/config teardown, best-effort like the bond removal already is
(a D-Bus failure here is logged, not fatal to the rest of the reset).
Without this, a previous owner's key would still be able to log in as `pi`
after a reset meant to return the appliance to factory defaults.

## Consequences

**Benefits:**
- No appliance ships with, or ever generates, a password shared across
  devices — the SEC-01 finding is fully closed, not just mitigated.
- SSH is entirely absent from the attack surface for the (likely common)
  user who never opens a shell — it doesn't just have a hard-to-find
  password, the daemon isn't even running.
- The privilege-escalation pattern (unprivileged process asks systemd, via a
  polkit rule scoped to one named unit, to run one oneshot script) is
  narrower than either existing precedent in this codebase and generalizes
  cleanly if a future feature needs the same shape.
- The physical UART console recovery path survives, with a real per-device
  secret instead of a shared one.

**Accepted trade-offs:**
- The documented dev workflow changes for everyone, including this
  project's own maintainers on their own hardware: `CLAUDE.md`'s SSH section
  no longer works with a fresh flash until SSH is turned on and a key added
  via the Portal once. Judged worth it — a workflow that only stays
  convenient by keeping the vulnerability alive isn't one worth preserving,
  and every image now exercises the exact code path a real user does.
- GitHub key import requires the appliance already have outbound internet
  (i.e., WiFi provisioning already completed) — it cannot be used during the
  initial AP-mode captive-portal setup. Manual key paste has no such
  requirement and is always available as a fallback.
- `/etc/issue`'s displayed password goes stale if the account password is
  ever changed locally (see above) — cosmetic only, not a security gap (the
  account's *actual* password is still whatever it was last set to).
- Adds `httpx` as a runtime dependency of `companion` (previously test-only)
  — already present in the lock file at the version pinned for tests, so
  this adds no new resolved package, only moves an existing one from `dev`
  to the main dependency list.
- ~~`GET /api/v1/ssh/status` can briefly show a state that was requested but
  never actually confirmed applied.~~ **Fixed.** `SshStatus`/
  `SshStatusResponse` now carry a `confirmed: bool` field. `SshAccessService.
  status()` derives it by comparing `ssh_status.json`'s mtime against the
  desired-state files' (`ssh_enabled`, `ssh_authorized_key`): the root unit
  always writes desired state first and only writes `ssh_status.json` once
  it (or its `EXIT` trap) has actually run, so a status file that predates
  the current desired state means the root side's report is missing or
  stale. This is a pure read-time check — it needs no boot-time
  reconciliation pass, since every call to `status()` re-derives `confirmed`
  from current file mtimes, catching both known failure paths: the Pi
  losing power mid-run of the root script (its `EXIT` trap, which normally
  rewrites `ssh_status.json` on every completion including failures, only
  fires on a clean exit — not a hard power loss, so a stale status can
  survive a reboot, but now reads back as `confirmed: false` rather than
  fact); and `systemd1_dbus.start_unit` itself timing out or erroring before
  the unit ever reports back (D-Bus down, polkit misconfigured) — in which
  case `ssh_status.json` is simply never written, so `confirmed` is `false`
  from the first `status()` call onward. `PUT /api/v1/ssh/settings` also now
  catches an `apply()` failure explicitly (it previously had no `try`/
  `except` around `await ssh.apply(...)`, so a D-Bus timeout there
  propagated as an unhandled 500) and still returns the `SshStatusResponse`
  shape with `confirmed: false`, rather than a bare 500. `authorized_keys`
  is unchanged: it's still always read live from companion's own key file,
  regardless of `confirmed` — that reflects what will be (or was just)
  requested, not what the root side has verified. Two things worth stating
  explicitly about how to read the resulting field combinations: (1)
  `confirmed: true` covers two different situations that both happen to
  need no follow-up action — "the root side's report is present and
  verified up to date" *and* "neither desired-state file has ever been
  written at all" (a factory-fresh appliance). The second case says nothing
  about `enabled`/`has_key` themselves (both default to `false`); it must
  not be read as "SSH is verified enabled," only as "nothing is left
  unconfirmed" — check `enabled`/`has_key` separately for the actual state.
  (2) When `confirmed` is `false`, `error` (like `enabled`/`has_key`/
  `applied_at`) is the *last confirmed report, if any exists* — not a live
  diagnosis of whatever request is currently pending. Concretely: if
  `ssh_status.json` exists but predates the current desired state (the
  stale-reboot case), its `error` can be left over from a *previous*,
  different request. `PUT /api/v1/ssh/settings` never writes its own
  `apply()` exception message into `error` — that failure surfaces only as
  `confirmed: false`, so a client shouldn't assume a non-null `error`
  explains the most recent call.

## Rejected alternatives

- **First-boot random password for SSH itself, printed to console**
  (the security review's first-choice recommendation) — rejected in favor of
  key-only Portal provisioning: SSH is a debug convenience for an appliance
  whose whole value proposition is "no terminal," so disabling it by default
  and provisioning it deliberately through the same interface everything
  else on the appliance already uses removes the attack surface rather than
  just rotating the secret that guards it.
- **Locking the `pi` account password entirely** (`passwd -l`) — considered
  for maximum simplicity, rejected because it removes the UART physical
  console fallback ADR-020 built the appliance around, with no equivalent
  replacement.
- **A full D-Bus broker service** for the privileged SSH operations — see
  "Rejected" note under Decision §3 above.
- **Per-image (not per-device) random password**, published in release
  notes — the security review's third-choice fallback. Rejected: still
  shares one password across every device built from the same image tag,
  which is weaker than every device getting its own.

Related: [ADR-020](020-appliance-hardening.md) (original "must be
re-evaluated before v1.0" flag, now resolved by this ADR — its Consequences
section is amended to point here rather than describing the old default),
[ADR-021](021-network-provisioning.md) and [ADR-038](
038-idle-battery-shutdown.md) (the polkit-scoping precedents this decision
narrows further), [ADR-028](028-audio-readiness-model.md) (the piecemeal-
privilege caution honored here, and the rejected broker-service alternative).
