# ADR-026 — Bluetooth Classic A2DP Pairing and Audio Readiness

**Status:** Accepted
**Date:** 2026-06-30
**Milestone:** M16

---

## Context

The appliance has two independent Bluetooth subsystems that must both be operational before audio can play:

- **BLE GATT** (`DeviceManager`) — connects to the speaker's LE interface for control commands (power, volume, device info). The speaker keeps its BLE radio active even in standby/off state, so this connection succeeds on every boot regardless of whether the speaker is ready for audio.
- **Bluetooth Classic A2DP** (`AudioService`) — connects to the speaker's BR/EDR interface to create the PipeWire audio sink that librespot uses for Spotify Connect output.

Before M16, the Portal had no visibility into A2DP state:

1. `AudioService` accepted a `sink_address` setting and exited immediately when it was `None`. The Supervisor then treated this as an unexpected exit and restarted it in an infinite backoff loop that served no purpose.
2. On a freshly flashed image, no A2DP address is configured. The Portal showed "Speaker: Connected" (BLE) and "Spotify Connect: Ready", but audio went to PipeWire's Dummy Output — silence.
3. The health pill showed "Speaker connected" even with the speaker powered off, because BLE ≠ audio readiness.
4. There was no user path to establish the Classic BT pairing from the Portal.

---

## Decision

### Two-state connection model

Explicitly separate BLE control connectivity from A2DP audio readiness in both the backend and the Portal:

- **Speaker row** — BLE GATT connection managed by `DeviceManager`. Reflects speaker control reachability.
- **Bluetooth Audio row** — A2DP connection managed by `AudioService`. Reflects audio readiness.
- **Health pill** — drives from `AudioService.status` rather than `DeviceManager.snapshot.connected`, because audio readiness is what matters to the user.

### AudioService waits for an address

`AudioService.run()` no longer returns immediately when no address is configured. Instead it waits on an `asyncio.Event` until `update_address()` is called. This allows the Supervisor to keep the service registered without spurious restart loops, and lets PairingService hand off the address in-process without a daemon restart.

### First-time pairing via PairingService

`PairingService` implements the one-shot pairing flow:

1. Takes a snapshot of all devices currently known to BlueZ (`bluetoothctl devices`).
2. Starts `bluetoothctl scan on` as a background process.
3. Polls `bluetoothctl devices` every 2 s, looking for new devices whose name contains "JBL".
4. For each new JBL device, calls `bluetoothctl info <mac>` and confirms the address type is `(public)` — BR/EDR devices use public addresses; LE-only devices use random addresses. This filters out the speaker's LE GATT address, which BlueZ already knows.
5. Once a candidate is found: stops the scan, runs `pair → trust → connect`.
6. On success: persists the MAC to `PortalConfig.audio_sink_address` via `ConfigStore`, then calls `AudioService.update_address()` to activate the service in-process.

### PortalConfig extended

`PortalConfig` gains `audio_sink_address: str | None = None`. On boot, `__main__.py` prefers this over the env-var `COMPANION_AUDIO__SINK_ADDRESS`. Users who set the env var retain backward compatibility; once the Portal pairing flow runs, the config file takes precedence.

### REST API surface

Two new endpoints under `/api/v1`:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/audio` | A2DP status: `{connected, address, pairing_state}` |
| `POST` | `/api/v1/audio/pair` | Start pairing scan (202 Accepted); 409 if already in progress |

`pairing_state` is one of `idle`, `scanning`, `pairing`, `failed`. The Portal polls `GET /api/v1/audio` every 2 s during an active pairing to track progress.

### Portal UX

The dashboard gains a **Bluetooth Audio** row between Speaker and Spotify Connect:

- **Not paired** (warn dot, "Pair Speaker" button) — no address configured
- **Scanning…** / **Pairing…** (spin dot) — pairing in progress
- **Pairing failed** (error dot, "Pair Speaker" button) — user can retry
- **Connecting…** (spin dot) — address known but A2DP link not yet established
- **Connected** (ok dot) — A2DP link is up; librespot has a real audio sink

The health pill now reflects audio readiness: "Speaker not paired", "Scanning…", "Pairing…", "Connecting audio…", or "Ready".

A **Pair Speaker** card appears below the dashboard when the button is clicked. It instructs the user to put the speaker in pairing mode, offers a **Start Pairing** button, and polls until success or failure.

---

## Why PairingService is not a Supervisor task

PairingService is an on-demand helper, not a long-running service. It creates a single `asyncio.Task` internally when `start()` is called, and the task self-terminates on success or failure. Registering it with the Supervisor (which is designed for persistent run loops) would be incorrect — the Supervisor would restart the pairing scan after every completion, including successful ones.

---

## Alternatives considered

### Store the address only in the env file

Rejected. Editing `/etc/companion/companion.env` and restarting the service is not a reasonable first-boot experience. The user would need SSH access and familiarity with the env file format.

### Use `bluetoothctl --transport bredr scan on`

Rejected as fragile — the `--transport` flag availability varies by bluetoothctl version. Filtering by address type via `bluetoothctl info` is portable and explicit about the selection criterion.

### Auto-detect BR/EDR address from the speaker's LE service data

The LE advertisement from the PartyBox 520 does not reliably encode the classic BT address in the expected AD type (`0x1B`). The scan-and-filter approach is more robust.

> **Correction ([ADR-027](027-bluetooth-bonding-architecture.md)):** this was checking the wrong AD structure. `0x1B` ("LE Bluetooth Device Address") is unrelated and absent; the BR/EDR address is reliably present in AD type `0x16` (Service Data) under Harman's vendor UUID `0xfddf`. ADR-027 makes LE service-data extraction the canonical discovery mechanism.

### Gate librespot on A2DP readiness (Spotify deregisters when audio unavailable)

Correct long-term behaviour, but deferred to M17.4. Gating Spotify on A2DP requires a dependency edge between `SpotifyService` and `AudioService` that the Supervisor does not yet model (see ADR-024 deferred design questions). M16 establishes the pairing flow and exposes audio state; M17 wires the lifecycle dependency. The `audio_ready` abstraction, event bus, Spotify gate, and A2DP connection management architecture are documented in [ADR-028](028-audio-readiness-model.md).

---

## Consequences

- First-time setup now has a clear, user-driven path: pair from the Portal, no SSH needed.
- The health pill reflects actual audio readiness rather than BLE-only connectivity, eliminating the false "Speaker connected" state when the speaker is off.
- `AudioService` and `PairingService` are clean enough to be tested without hardware; subprocess calls are isolated behind helper functions that can be patched.
- `PortalConfig` schema is extended; existing config files without `audio_sink_address` are backwards-compatible (Pydantic defaults the field to `None`).
