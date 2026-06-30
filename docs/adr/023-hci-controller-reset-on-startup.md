# ADR-023 — HCI Controller Reset at Startup via hciconfig

**Status:** Accepted
**Date:** 2026-06-30
**Milestone:** M13.1

---

## Context

During M13.1 hardware validation, the BCM Bluetooth controller on the Pi entered a wedged state after many consecutive failed GATT connection attempts. In the wedged state every new LE connection fails immediately at the LE Read Remote Used Features exchange with `Connection Failed to be Established (0x3e)` — a condition that persists across `partybox-companion` process restarts but clears with an HCI-level reset (`hciconfig hci0 reset` or `hciconfig hci0 down && up`).

The companion process runs as the dedicated `companion` user, which holds only `CAP_NET_BIND_SERVICE`. An HCI reset requires `CAP_NET_ADMIN`. An earlier version of `__main__.py` attempted the reset in Python via a subprocess call, but it failed silently (exit code 1) and logged a misleading warning.

The controller wedge is a startup-time problem: it accumulates during a session with many failed attempts and is cleared on the next clean start. The question is where and how to issue the reset reliably.

---

## Decision

The reset is issued as an `ExecStartPre` directive in `companion.service`:

```ini
ExecStartPre=+/usr/bin/hciconfig hci0 reset
```

The `+` prefix runs the command as root regardless of `User=companion`, satisfying the `CAP_NET_ADMIN` requirement without elevating the main process.

**Tool choice — `hciconfig` over `btmgmt`:** `hciconfig` is deprecated upstream (BlueZ ≥ 5.56 recommends `btmgmt`), but:

- There is no direct `btmgmt` command that issues a bare HCI Reset without also disconnecting all active connections. The closest equivalent (`btmgmt power off && btmgmt power on`) is semantically heavier and risks race conditions with NetworkManager's Bluetooth stack.
- `hciconfig` still ships in the `bluez` package on Raspberry Pi OS Bookworm and its behaviour on this specific operation is well-understood.
- The operation is a single line in a systemd unit; the blast radius of a future migration is small.

**Scope limitation:** This reset only runs at `ExecStartPre` time. It does not address a controller wedge that develops during long-running operation (e.g. after an extended failed-reconnect cycle while the appliance is running). That case is explicitly deferred to M17 (Reliability), which will implement runtime Bluetooth recovery without requiring a process restart.

---

## Alternatives considered

### Python subprocess in `__main__.py`

The process user lacks `CAP_NET_ADMIN`. Silent failure and misleading log output. Removed in favour of the systemd approach.

### Grant `CAP_NET_ADMIN` to the companion user

Would allow the Python-level reset to work, but `CAP_NET_ADMIN` is broad — it also grants the ability to reconfigure network interfaces, manipulate routing tables, and other operations irrelevant to the appliance. Rejected; the principle of least privilege favours a one-shot privileged pre-start command over a permanently elevated service.

### `btmgmt power off / power on`

Semantically heavier than an HCI Reset; tears down active connections managed by the Bluetooth daemon. Carries a risk of interfering with Classic audio sessions already in progress at the time of restart. Rejected for now; revisit if `hciconfig` is removed from Pi OS.

### Skip the reset; rely only on Restart=on-failure

The wedge clears on the next host reboot, but `Restart=on-failure` restarts the process without a host reboot — the wedged controller persists. The service would restart into the same broken state indefinitely. Rejected.

---

## Consequences

- Startup adds one synchronous `hciconfig hci0 reset` call (< 100 ms, no side effects on a fresh boot).
- The `companion` process always starts with a known-good controller state.
- A future migration from `hciconfig` to `btmgmt` (or a bluetoothctl sequence) is a deliberate change in this one file; no Python code is involved.
- Runtime Bluetooth recovery (wedge during operation, not at startup) remains unaddressed until M17.
