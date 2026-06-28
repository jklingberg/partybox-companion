# ADR-018 — systemd Service Model

**Status:** Accepted
**Date:** 2026-06-28
**Milestone:** M12

---

## Context

The appliance must start on boot, restart after crashes, shut down cleanly, and produce logs accessible to operators without SSH. This ADR records the systemd integration decisions.

---

## Decision

### Service name

`companion.service` — short, matches the product name on a dedicated single-purpose appliance. Daily operations (`systemctl restart companion`, `journalctl -u companion`) are ergonomic. There is no ambiguity on an appliance that runs exactly one named service.

### Service type

`Type=simple`. The process does not fork and does not implement `sd_notify`. systemd considers the service started the moment the process starts. The HTTP server becomes ready within a few seconds; no ordering constraint requires systemd to know the exact moment.

**Future improvement:** `Type=notify` with a `sd_notify(READY=1)` call once uvicorn is listening. This would allow dependent services to wait for the Portal to be ready. Deferred to post-v1.0.

### User and groups

The service runs as a dedicated `companion` user in the `bluetooth` group. The `bluetooth` group membership is required for BlueZ D-Bus access and `bluetoothctl` calls in `AudioService`.

The `companion` user has no login shell and no home directory. The `StateDirectory=companion` directive creates and owns `/var/lib/companion` before the process starts.

### Restart policy

`Restart=on-failure` with `RestartSec=5`. The service restarts after unexpected exits (crash, OOM kill) but not after a clean `systemctl stop`. `RestartSec=5` prevents restart storms.

### Startup ordering

```ini
After=network-online.target bluetooth.service
Wants=network-online.target bluetooth.service
```

`Wants=` (not `Requires=`) — the service starts regardless of whether network or Bluetooth is immediately available. `DeviceManager` and `AudioService` have their own retry loops that tolerate a Bluetooth adapter that isn't ready at the moment the process starts. The Portal is accessible as soon as uvicorn binds, even before any speaker connection.

### Shutdown

`TimeoutStopSec=30`. systemd sends `SIGTERM` first; uvicorn's graceful shutdown cancels the asyncio tasks. `SpotifyService._terminate()` allows up to 5 s for librespot to exit cleanly. 30 s is ample headroom before the fallback `SIGKILL`.

### Logging

`StandardOutput=journal` and `StandardError=journal` with `SyslogIdentifier=companion`. All output from the process — including librespot's stderr, which is streamed into the Python logger — is captured by journald.

The Python logging format is adapted at runtime: when `JOURNAL_STREAM` is present in the environment (set by systemd), timestamps are omitted from the log format because journald provides them. In a terminal (development), the full format with timestamps is used.

Log level is controlled by `COMPANION_LOG_LEVEL` in the environment (default `INFO`).

### Security hardening

```ini
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
```

`ProtectSystem=strict` makes `/usr`, `/boot`, and `/etc` read-only for the service. `/var` remains writable; the `StateDirectory=companion` directive whitelists `/var/lib/companion`.

### Port 80

The production appliance exposes the Portal on port 80. The mechanism for a non-root process to bind port 80 — `AmbientCapabilities=CAP_NET_BIND_SERVICE`, a reverse proxy, or an iptables redirect — is an implementation decision deferred to M14 (First Boot Experience), when the full networking stack is assembled. The service unit contains a comment noting this gap; M14 will add the chosen mechanism.

---

## Alternatives considered

### `partybox-companion.service` as service name

Matches the executable name exactly. Advantages: unambiguous in a multi-service environment. Disadvantages: verbose for daily administration on a single-purpose appliance. Rejected in favour of `companion.service`.

### `Type=forking`

Would require the process to `fork()` into the background — the opposite of what uvicorn does. Rejected.

### Running as `root`

Simple but provides no security boundary. An exploit in the HTTP server or Bluetooth stack would have full system access. Rejected in favour of a dedicated user.

### Running as `pi`

The default Raspberry Pi OS user already has Bluetooth access. Convenient for development but grants the service unnecessary privileges (login shell, sudo access in stock Pi OS). Rejected in favour of a dedicated `companion` user.

---

## Consequences

- The appliance runs without any open SSH session or manual commands.
- Crashes are recovered automatically in 5 s.
- `systemctl status companion` is the primary health check.
- `journalctl -u companion` is the primary log access method.
- The image build (M13) must create the `companion` user and install the unit file.
- Port 80 binding mechanism is unresolved; see M14.
