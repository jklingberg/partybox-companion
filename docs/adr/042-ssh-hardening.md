# ADR-042: SSH Hardening — No Shared Default Credentials

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

- Toggle SSH on/off.
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

**Enabling with no key configured is rejected outright** (`ValueError` in
`SshAccessService.apply`, surfaced as 400 by the router) — sshd would just
come up with `PasswordAuthentication no` and nothing that can authenticate,
which is a confusing dead end rather than a safe default.

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

`StartUnit` is fire-and-forget from Companion's point of view (matching the
existing WiFi-provisioning UX: `PUT /api/v1/ssh/settings` returns
immediately, the Portal re-polls `GET /api/v1/ssh/status` the same way it
already polls `GET /api/v1/wifi/status`). The oneshot unit's own work is a
handful of file/systemctl operations and completes in well under a second in
practice.

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
