---
name: pi-hardware-ops
description: SSH access, deploying source changes, service/log commands, and restarting the speaker or the Pi for the appliance Raspberry Pi. Use when troubleshooting a physical PartyBox Companion appliance, enabling SSH, rsyncing package changes to /opt/partybox-companion, checking companion.service status/logs, or restarting the speaker/Pi.
---

## Raspberry Pi (hardware)

### SSH access

The appliance Pi is normally reachable at `pi@partybox.local` (mDNS) or `pi@partybox` (router DNS), but **neither is guaranteed** — both depend on network/client behavior outside this project's control, not on anything Companion configures:

- `partybox.local` requires the *client's* OS to have a working mDNS resolver (reliable on macOS/most Linux; not guaranteed on Windows without Bonjour; often blocked on guest/corporate VLANs that filter multicast). It can and does stop resolving with no change on the Pi side (observed 2026-07-18: `DNS_PROBE_FINISHED_NXDOMAIN` in Chrome with no appliance-side fault) — a client-side or router-side mDNS hiccup, not an appliance bug.
- `partybox` (no `.local`) depends on the *router* auto-registering the DHCP client hostname in its local resolver. Most consumer routers do this, but it's still router-specific behavior, not a protocol guarantee.

If either stops resolving, don't treat it as an appliance fault — first try the other, then fall back to the Pi's IP address (check your router's device list, or a reservation if one is configured) and use that IP directly for the SSH/rsync/curl commands below in place of `partybox.local`.

**SSH ships disabled on every image, with no default password** ([ADR-043](../../../docs/adr/043-ssh-hardening.md) — the `pi`/`raspberry` shared credential and `PasswordAuthentication yes` this section used to document are gone; this applies to dev-flashed images exactly like release ones, there is no build flag that restores the old behavior). Before any SSH/rsync command below will work on a given device, one time per device:

1. Open the Portal (`http://partybox.local`) → **Settings → SSH access**.
2. Either paste your public key or enter your GitHub username and click **Import** (fetches `https://github.com/<username>.keys`, the same public endpoint `ssh-import-id`/cloud-init use — requires the appliance already be on WiFi, since it needs outbound internet).
3. Click **Save**. There is no separate enable toggle — having a key configured *is* what turns SSH on; click **Disable SSH** to clear the key(s) and turn it back off.

After that, connect with your own key — no password, no `sshpass`:

```bash
# One-off command
ssh -o StrictHostKeyChecking=no pi@partybox.local "<command>"

# rsync
rsync -e "ssh -o StrictHostKeyChecking=no" -av --delete <src> pi@partybox.local:<dst>
```

`StrictHostKeyChecking=no` avoids an interactive host-key prompt on first contact.

The `pi` account still has a password, but it's random per device (generated on first real boot, never at image-build time — see ADR-043) and is for the **physical/UART console only**; it is never accepted over SSH (which stays key-only whenever it's enabled at all). If you need it, it's printed to `/etc/issue`, visible on the serial console or a directly attached keyboard/monitor.

### Port 22 answers but no key is accepted yet

If `ssh`/`rsync` to the Pi gets a TCP response on port 22 (banner exchange happens, connection isn't refused/timed out) but every attempt ends in `Permission denied (publickey)`, that means SSH is enabled on the appliance but it doesn't yet trust any key you're offering — not a network or appliance fault, so don't troubleshoot mDNS/routing for this symptom. Since ADR-043 removed password auth entirely, the only way in is to get a public key into the Portal's SSH settings, and there is no key present on a fresh devcontainer by default. Do this:

1. Check for a devcontainer-shared key first, at `$CLAUDE_CONFIG_DIR/ssh/partybox_ed25519` (i.e. `/home/vscode/.claude/ssh/partybox_ed25519`). This lives inside the `partybox-claude-config` named volume mounted at `CLAUDE_CONFIG_DIR` (see `.devcontainer/devcontainer.json`), so it survives container rebuilds and is already visible to every Claude Code session sharing this container — reuse it, don't generate a second one.
2. If it isn't there yet, generate it once, non-interactively, no passphrase:
   ```bash
   mkdir -p "$CLAUDE_CONFIG_DIR/ssh"
   ssh-keygen -t ed25519 -N "" -f "$CLAUDE_CONFIG_DIR/ssh/partybox_ed25519" -C "claude-devcontainer-partybox"
   ```
3. Show the user the public key and ask them to install it — this is the "paste your public key" path from the SSH access steps above, not the GitHub-username import (that imports the *user's* GitHub-linked keys, not this generated one):
   ```bash
   cat "$CLAUDE_CONFIG_DIR/ssh/partybox_ed25519.pub"
   ```
   Tell the user: open the Portal (`http://partybox.local`) → **Settings → SSH access** → paste the printed key into the public-key field → **Save**.
4. Once they confirm it's applied, retry using that identity file explicitly:
   ```bash
   ssh -o StrictHostKeyChecking=no -i "$CLAUDE_CONFIG_DIR/ssh/partybox_ed25519" pi@partybox.local "<command>"
   ```
   For the rest of the session, add `-i "$CLAUDE_CONFIG_DIR/ssh/partybox_ed25519"` to the `$SSH`/`$RSYNC` commands used elsewhere in this skill so the user isn't prompted again.

### `pi` vs `companion`: two separate users

SSH always connects as `pi`, but the appliance service runs as `companion` — a **different, more restricted account**. This is deliberate (see [ADR-019](../../../docs/adr/019-distribution-approach.md)), not an oversight, so don't try to "fix" it by running things as `pi` or `root` — expect the split and work with `sudo` instead.

- `pi` — interactive login user (`useradd -m -s /bin/bash -G sudo pi`), passwordless sudo, has a home directory, owns the WirePlumber/PipeWire audio session at `/run/user/1000/`.
- `companion` — system account (`useradd --system --no-create-home --shell /usr/sbin/nologin`), **no shell, cannot log in**. Runs `companion.service` under systemd hardening (`NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=read-only`) plus `CAP_NET_BIND_SERVICE` to bind port 80. It has **no sudoers grants at all** — `NoNewPrivileges` blocks the setuid escalation `sudo` needs, so one would silently fail to run anyway. A `systemctl --user -M pi@ restart wireplumber` sudo grant was proposed early on for `AudioService` to self-heal WirePlumber, but was never actually implemented and the doc claim lingered stale until corrected (see `docs/validation/runs/2026-07-02-rc13.md`); detection-only remains the real v1.0 posture there. Where `companion` genuinely needs a privileged operation, it goes through **D-Bus + a narrow polkit rule** installed by `install.sh` instead: `org.freedesktop.NetworkManager.*` for provisioning ([ADR-021](../../../docs/adr/021-network-provisioning.md)), `org.freedesktop.login1.power-off`(-multiple-sessions) for the idle-battery-shutdown watcher ([ADR-038](../../../docs/adr/038-idle-battery-shutdown.md)), and `org.freedesktop.systemd1.manage-units` — scoped to exactly the `companion-ssh-apply.service` unit name — for the Portal's SSH access flow ([ADR-043](../../../docs/adr/043-ssh-hardening.md)).

Ownership map — files under these paths are **not** readable/writable by `pi` without `sudo`:

| Path | Owner | Contents |
|---|---|---|
| `/var/lib/companion/` | `companion` | Portal state (`config.json`) |
| `/run/companion/` | `companion`, mode 0700 | Runtime dir (`XDG_RUNTIME_DIR` for the companion process) |
| `/etc/companion/companion.env` | `root` | Operator env overrides |
| `/run/user/1000/` | `pi` | PipeWire-pulse socket; chmod'd to 755 at service start so `companion` can reach it — see `companion.service` |

Practical commands when troubleshooting over SSH as `pi`:

```bash
# Read/list a companion-owned path
$SSH pi@partybox.local "sudo cat /var/lib/companion/config.json"
$SSH pi@partybox.local "sudo ls -la /run/companion"

# Run a one-off command as companion (works despite the nologin shell —
# sudo execs the command directly, it doesn't need an interactive login)
$SSH pi@partybox.local "sudo -u companion <command>"
```

Never attempt `ssh companion@partybox.local` — there is no shell to log into.

### Deploying source changes to the Pi

The appliance venv lives at `/opt/partybox-companion/` and is a `--no-editable` install (source copied into site-packages). To deploy a change without rebuilding the full image, rsync the relevant package directly into site-packages and restart the service.

Site-packages is **root-owned** on release images, so the remote rsync must run under sudo (`--rsync-path="sudo rsync"`); a plain rsync fails with `Permission denied (13)`.

SSH must already be enabled and your key added via the Portal (see "SSH access" above) before any of this works.

```bash
SSH="ssh -o StrictHostKeyChecking=no"
RSYNC="rsync -e 'ssh -o StrictHostKeyChecking=no' --rsync-path='sudo rsync'"

# Deploy companion package changes
$RSYNC -a --delete --exclude='__pycache__' packages/companion/src/companion/ \
    pi@partybox.local:/opt/partybox-companion/lib/python3.14/site-packages/companion/

# Deploy partyboxd package changes
$RSYNC -a --delete --exclude='__pycache__' packages/partyboxd/src/partyboxd/ \
    pi@partybox.local:/opt/partybox-companion/lib/python3.14/site-packages/partyboxd/

# Deploy partybox SDK changes
$RSYNC -a --delete --exclude='__pycache__' packages/partybox/src/partybox/ \
    pi@partybox.local:/opt/partybox-companion/lib/python3.14/site-packages/partybox/

# Restart the service after any change
$SSH pi@partybox.local "sudo systemctl restart companion"
```

This is sufficient for Python source changes. For dependency changes (`pyproject.toml`, `uv.lock`) or changes to `install.sh`-managed files (systemd unit, BlueZ config, Avahi record), a full image rebuild and reflash is required.

### Service and log commands

```bash
SSH="ssh -o StrictHostKeyChecking=no"

# Service status
$SSH pi@partybox.local "systemctl status companion"

# Restart
$SSH pi@partybox.local "sudo systemctl restart companion"

# Health check
$SSH pi@partybox.local "curl -s http://localhost/api/v1/health"

# Follow logs
$SSH pi@partybox.local "journalctl -u companion -f"

# Last 100 lines
$SSH pi@partybox.local "journalctl -u companion -n 100 --no-pager"

# Bluetooth adapter reset (if GATT connections fail but scanning works)
$SSH pi@partybox.local "sudo systemctl restart bluetooth"
```

### Restarting the speaker or the Pi

**Speaker restart** — there is no dedicated restart endpoint; power-cycle it with the existing power endpoints (`packages/partyboxd/src/partyboxd/api/routes.py`):

```bash
curl -X POST -H "X-Api-Key: your-key" http://partybox.local/api/v1/power/off
sleep 2
curl -X POST -H "X-Api-Key: your-key" http://partybox.local/api/v1/power/on
```

Omit the `X-Api-Key` header if the appliance has no `api_key` configured (the default — auth is opt-in).

**Pi restart** is *not* exposed via the REST API — only the `companion` service can be restarted remotely (`sudo systemctl restart companion`, above). To reboot the underlying OS, use SSH directly:

```bash
$SSH pi@partybox.local "sudo reboot"
```
