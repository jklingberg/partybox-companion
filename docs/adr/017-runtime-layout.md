# ADR-017 — Production Runtime Layout

**Status:** Accepted
**Date:** 2026-06-28
**Milestone:** M12

---

## Context

From M6 through M11, `partybox-companion` ran as a manually started foreground process — typically via `uv run` in an SSH session. M12 transitions the appliance to a proper production service. This ADR records the agreed filesystem layout and configuration ownership model for the production installation.

---

## Decision

### Filesystem layout

The appliance follows the [Filesystem Hierarchy Standard](https://refspecs.linuxfoundation.org/FHS_3.0/fhs-3.0.html) and Debian/Raspberry Pi OS conventions:

```
/opt/partybox-companion/
    bin/partybox-companion          # entry point
    lib/python3.x/site-packages/   # partybox, partyboxd, companion, all dependencies

/etc/companion/
    companion.env                   # operator overrides (EnvironmentFile for systemd)

/var/lib/companion/
    config.json                     # Portal config — written by the running appliance

/lib/systemd/system/
    companion.service               # the service unit

/var/log/journal/                   # journald — no separate log files
```

The template for `companion.env` lives in the repository at `system/systemd/companion.env`.

### Why `/opt/partybox-companion/`

FHS §3.13 defines `/opt/` as the location for "add-on application software packages" — self-contained software not installed as part of the base OS. This is precisely what the appliance is: a complete, vendored Python venv that we build and control.

- `/usr/lib/` is for shared libraries required by OS-installed programs. A self-contained application venv is not a library.
- `/usr/libexec/` is for helper executables not meant for direct user invocation.
- `/usr/local/` implies "installed by the local sysadmin, not the distro". On an image we build ourselves, that distinction is meaningless.
- `/opt/partybox-companion/` is self-describing, isolated from the system Python, and upgradeable by replacing the directory atomically. Appliance-grade software on Raspberry Pi OS (RealVNC, Raspberry Pi Connect) uses this pattern for exactly the same reasons.

---

## Configuration ownership

This is a load-bearing architectural principle:

| Location | Owner | Meaning |
|---|---|---|
| `/etc/companion/companion.env` | Image / installer / operator | Machine-level deployment settings. Set once. Rarely changed. Never modified by the running appliance. |
| `/var/lib/companion/config.json` | The running appliance | User configuration. Written exclusively by the Portal via `PUT /api/v1/config`. Never modified by operators directly. |

**The Portal never modifies `/etc/companion/companion.env`.** The env file is for deployment-time decisions (which Bluetooth MAC to use, what port to bind). The config file is for user preferences (device name, Spotify name, bitrate). These are different domains with different lifecycles and different owners.

This separation means:
- Upgrading the appliance (replacing `/opt/partybox-companion/`) leaves both config locations untouched.
- Reflashing the OS (which wipes `/etc/` and `/var/`) returns to factory defaults, as expected.
- The Portal's config survives service restarts, reboots, and software upgrades automatically.

---

## Data directory

`/var/lib/companion/` is the single mutable runtime directory. The `StateDirectory=companion` directive in the systemd unit creates it automatically and sets ownership to the `companion` user before `ExecStart`. No `mkdir` is needed in any setup script.

`ConfigStore` writes `config.json` here. This is the only file the running process modifies.

---

## Environment file

`/etc/companion/companion.env` is loaded by systemd as an `EnvironmentFile` (with a `-` prefix — ignored if absent). The service unit hardcodes one production-mandatory value directly so it is always set even if the file is missing:

```ini
Environment=COMPANION_DATA_DIR=/var/lib/companion
```

All other runtime tuning (sink address, port, log level, Spotify name) lives in the env file so operators can adjust without editing the unit or the application code.

---

## Logging

No log files are written. The process logs to stdout; systemd routes it to journald via `StandardOutput=journal`. Operators use `journalctl -u companion` for log access. The `GET /api/v1/debug/bundle` endpoint includes recent journal lines in the downloadable ZIP for support use.

---

## Consequences

- **Development defaults are preserved.** `CompanionSettings.data_dir` defaults to `~/.local/share/companion` in code. The systemd unit overrides this with `COMPANION_DATA_DIR=/var/lib/companion`. Developers running without the unit get the old path — no change to the dev workflow.
- **Port 80** is the production target. The mechanism (capability, reverse proxy, iptables) is M14's decision. The code default (8080) is used until M14.
- **Upgrade path is straightforward.** Replace `/opt/partybox-companion/`, restart the service. Config and state in `/var/lib/companion/` are untouched.
