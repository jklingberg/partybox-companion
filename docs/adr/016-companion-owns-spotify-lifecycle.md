# ADR-016: Companion Owns the Spotify Connect Lifecycle

**Status:** Accepted

---

## Context

librespot is the open-source Rust implementation of Spotify Connect. The most common way to deploy it on a Raspberry Pi is via Raspotify — a Debian package that wraps librespot in a `systemd` service (`raspotify.service`) and manages its configuration via `/etc/raspotify/conf`.

When adding Spotify Connect in M9, the question arose: who should own the librespot process lifecycle?

Two options were considered:

1. **Delegate to systemd** — install `raspotify.service`, let systemd start and restart librespot, and have the daemon observe it (via `systemctl status` or D-Bus).
2. **Companion owns the process** — the daemon starts librespot directly as a subprocess, monitors its output, and restarts it on failure.

---

## Decision

The Companion daemon (`partybox-companion`) is the sole orchestrator of the librespot process. It starts librespot on boot, monitors its stderr for playback state, restarts it after unexpected exits, and terminates it cleanly on shutdown.

Concretely:

- `companion` runs librespot via `asyncio.create_subprocess_exec`
- `SpotifyService` owns the entire lifecycle: start → monitor → restart → terminate
- `raspotify.service` must not be started or enabled — it is a second orchestrator and must not conflict
- Companion configuration (`COMPANION_SPOTIFY__*`) drives librespot arguments directly; no Raspotify config files are read

Raspotify may be used **during development** as a convenient source of a prebuilt librespot binary for the target architecture. This is an implementation detail — a shortcut to get a binary without a Rust toolchain on the Pi. The `raspotify.service` unit should be disabled immediately after package installation.

Before v1.0, librespot must ship as part of Companion. Users install one package; librespot is an internal dependency, not a separate product they are expected to know about or manage.

---

## Consequences

**Benefits:**

- **Single orchestrator.** The daemon knows the current process state (running, active, crashed) without consulting systemd or any external service manager. `SpotifyService.status` is always authoritative.
- **Clean shutdown.** When `partybox-companion` stops, librespot is terminated. No orphan processes, no manual cleanup.
- **No systemd dependency in application code.** The daemon is a Python asyncio process. It does not require root, D-Bus access, or systemd knowledge to manage its child processes.
- **Consistent with the appliance philosophy** (see [ADR-005](005-appliance.md)): the appliance is `partybox-companion`. Users do not install a collection of services and configure them individually.
- **Testable in CI.** `SpotifyService` can be tested with a mock binary or by patching `shutil.which`. A systemd-backed lifecycle cannot be tested without a real init system.

**Accepted trade-offs:**

- If `partybox-companion` crashes, librespot is orphaned until the OS or a watchdog cleans it up. This is mitigated by a process supervisor (`systemd`, `s6`, or similar) restarting `partybox-companion` — the companion is the supervised unit, not librespot.
- The companion must bundle or co-install a librespot binary for the target architecture, which adds distribution complexity. This is intentional: complexity sits in packaging, not in user-facing setup steps.

**Rejected alternative:** Delegating lifecycle to `raspotify.service`.

This would require the daemon to call `systemctl start/stop/status raspotify`, which: (a) requires root or `sudo` capability, (b) couples the daemon to systemd as a runtime dependency, (c) makes the running state observable only via D-Bus or subprocess calls rather than in-process, and (d) exposes Raspotify as a user-visible concept in documentation, error messages, and troubleshooting guides — contradicting the "complete appliance" positioning in ADR-005.

---

## Distribution corollary

The architectural requirement for v1.0 is:

> **Users install Companion — not librespot.**

The mechanism — bundled binary, Debian dependency, image pre-installation, or another approach — is an implementation decision for the packaging milestone. The constraint is that a user who flashes the Companion image and boots the device should find Spotify Connect working without any additional installation steps.
