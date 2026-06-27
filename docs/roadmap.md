# Roadmap

## Current status

**M1 — Foundation** and **M2 — Bluetooth Transport** are complete. The BLE GATT
control transport, LE scanner, and mock are in place, with the public connect +
power-on path verified end-to-end against a real PartyBox 520.

Work is beginning on **M3 — Audio Transport Viability**.

---

## Design filter

Before adding anything to a milestone, ask:

> *Does this make the PartyBox a better WiFi speaker in a way that Spotify Connect, AirPlay, or Bluetooth AVRCP cannot?*

If the answer is no, it does not belong in the MVP. Volume, play/pause, and skip are already handled well by librespot and shairport-sync. The SDK focuses on what those protocols cannot provide: power management, battery, device information, lighting, and other hardware-unique features.

---

## Milestones

### M1 — Foundation ✅

Repository scaffold, monorepo structure, CI pipeline, shared tooling (ruff, mypy, pytest), and architecture documentation.

**Done when:** `uv sync` works, CI is green, and the architectural intent of the project is documented.

---

### M2 — Bluetooth Transport ✅

**Package:** `partybox`

`ControlTransport` ABC, `BleakTransport` (BLE GATT via bleak), `MockTransport` for testing, and `scanner.py` for LE device discovery.

> **Transport correction (see [ADR-015](adr/015-bluetooth-control-transport.md)):** M2 originally assumed Bluetooth Classic SPP/RFCOMM. Hardware verification showed speaker control is **BLE GATT** — there is no RFCOMM service. The backend uses `bleak`; discovery scans LE.

Getting the transport right matters more than getting it fast. The `MockTransport` must be good enough that the entire protocol and device layers can be developed and tested without real hardware.

**Done when:** A real PartyBox can be discovered and connected to from a Python script; a `MockTransport` allows the same code path to be exercised in CI.

---

### M3 — Audio Transport Viability

A technical viability spike. This milestone answers the most critical architectural question before any higher-level features are built:

> *Can a Raspberry Pi reliably stream music to a JBL PartyBox over Bluetooth A2DP while simultaneously maintaining the BLE control connection?*

This is not an architecture milestone. The goal is to produce evidence, not production code. No daemon abstractions, no REST API, no configuration system — only the minimum needed to answer the question.

**What this milestone validates:**

- BlueZ pairs with the PartyBox as an A2DP sink
- PipeWire routes librespot output to the A2DP connection reliably
- Audio is delivered at acceptable quality with no audible dropouts
- The BLE control connection (from M2) remains stable while A2DP is active
- Power-on/off commands work while audio is playing
- Automatic reconnection works after standby
- The combination is stable over long-running sessions (30+ minutes)

**Scope:**
- Minimal script to establish BLE control + A2DP connections simultaneously
- librespot running in PipeWire output mode, routed to the Bluetooth device
- Extended playback test: continuous session of 30+ minutes

**Not in scope:** daemon architecture, REST API, Portal, configuration system, subprocess abstractions.

**Done when:** Music plays through the PartyBox from Spotify Connect via the Pi for 30+ minutes without audible dropouts. The BLE control connection remains active throughout. Power-on/off works while audio is playing. Reconnection after standby works.

**If this milestone fails:** The overall architecture must be reconsidered before investing further in the appliance design.

---

### M4 — Protocol Foundation

**Package:** `partybox`

Frame codec, message dataclasses, parser, and serializer. Initial message coverage is intentionally narrow: **power commands and device information only**.

The protocol layer is stateless and pure. Every message type is a dataclass; the parser and serializer are pure functions. No I/O at this layer.

Message coverage is extended incrementally in later milestones as capabilities are added. Do not preemptively implement messages that no milestone yet needs.

**Done when:** A power-on command can be encoded and sent; a device info response can be decoded into a typed message. Protocol bytes are captured as test fixtures so CI runs without hardware.

---

### M5 — Core Device Capabilities

**Package:** `partybox`

`Device` ABC, `PartyBoxDevice` implementation, and the **three capabilities needed for the WiFi speaker MVP**:

| Capability | Always present | Justification |
|---|---|---|
| `PowerCapability` | yes | power on/off is hardware-unique |
| `DeviceInfoCapability` | yes | firmware version, model name |
| `BatteryCapability` | no (portable models only) | battery status is hardware-unique |

Event stream (`device.events()`) for daemon integration.

**Out of scope for M5:** volume, input source, lights, EQ, microphone. These do not contribute to the WiFi speaker MVP.

**Done when:** `await speaker.power.turn_on()`, `await speaker.device_info.firmware_version()`, and (on supported models) `await speaker.battery.level()` work against a real device. `device.events()` yields typed events.

---

### M6 — Daemon

**Package:** `partyboxd`

Internal event bus, daemon lifecycle, and a minimal HTTP skeleton. The daemon owns the Bluetooth connection and re-emits device events to registered handlers.

**Done when:** `partyboxd --config ...` starts, connects to a real PartyBox, and serves `GET /api/v1/status` returning power state, battery (if available), and firmware version as JSON.

---

### M7 — Companion Portal MVP

**Package:** `companion`

The Portal is introduced here — immediately after the daemon — because it is the primary onboarding surface, not a finishing touch. A user who has just booted the device should be able to verify their speaker connection and configure the appliance from a browser. See [ADR-011](adr/011-companion-portal.md) and [ADR-013](adr/013-user-journey-milestone-ordering.md).

M6 already exposes a minimal status endpoint (`GET /api/v1/status`). The Portal MVP is built against that endpoint — it does not need the full REST API.

**This milestone covers:**
- First-boot setup wizard: Bluetooth verification, speaker pairing, basic service configuration
- Status view: speaker connection, power state, battery level, firmware version, daemon health
- Basic configuration: device name, service preferences (written to config file)
- Spotify Connect and AirPlay sections present but showing "not yet active"

The Portal MVP is intentionally read-heavy. Control actions (power on/off from the browser) require the full REST API, which arrives in M8.

**Network prerequisite:** The Portal assumes the Pi already has network connectivity. For v1.0, WiFi is configured by writing credentials to the SD card before first boot — Raspberry Pi OS supports `wpa_supplicant.conf` on the boot partition, requiring only a file editor and no terminal on the Pi itself. A hotspot/captive-portal mode is post-v1.0.

The Companion Portal does **not** include media playback controls.

**Done when:** A user who has booted the device (with network reachable) can verify speaker status and complete initial configuration from a browser without touching a terminal on the Pi.

---

### M8 — REST API & CLI

**Packages:** `partyboxd` (REST API) · `companion` (CLI)

Full REST API for the M5 capabilities, WebSocket event stream, and API key authentication. The `partybox` CLI binary. The Portal's control actions (power on/off, etc.) become available once the REST API is in place.

```
partybox status           # power state, battery, firmware
partybox power on/off
partybox watch            # stream device events
```

**Done when:** Power, device info, and battery endpoints are stable; WebSocket delivers events; API key auth works; CLI commands work end-to-end against a running daemon.

---

### M9 — Spotify Connect

**Package:** `companion`

librespot subprocess manager. Start on boot, restart on crash, stop on Bluetooth disconnect. The Portal's Spotify section (introduced in M7 as a placeholder) becomes active.

**Done when:** A Spotify client sees the PartyBox as a Connect device; playback starts and stops correctly; the daemon event stream reflects playback state; Portal shows Spotify as active.

---

### M10 — AirPlay

**Package:** `companion`

shairport-sync subprocess manager. The Portal's AirPlay section (introduced in M7 as a placeholder) becomes active.

**Done when:** An Apple device sees the PartyBox as an AirPlay receiver; Portal shows AirPlay as active.

---

### M11 — Companion Portal: Complete

**Package:** `companion`

The Portal is completed with full Spotify/AirPlay configuration flows, diagnostics, and administration. This milestone closes the gap between the minimal Portal introduced in M7 and the full appliance experience.

**This milestone adds:**
- Spotify Connect and AirPlay configuration in the Portal (device name, settings)
- Full diagnostics: connection history, Bluetooth diagnostics
- Log download and debug bundle generation
- Full configuration management: network settings, update channel

**Done when:** All Portal sections are complete; a user can configure Spotify Connect and AirPlay from the Portal; log download works.

---

### v1.0

**Release criteria:**

- A non-technical user can flash the image and boot the device.
- Complete the initial setup in the Companion Portal without opening a terminal.
- Start streaming via Spotify Connect.
- Start streaming via AirPlay.
- Recover automatically after a reboot.

---

## Deliberately deferred

| Feature | Reason |
|---|---|
| Volume control via SDK | librespot and shairport-sync handle volume within their protocols. Direct hardware volume adds no value for the WiFi speaker use case in v1.0. |
| Input source selection | Useful, but not needed to stream Spotify or AirPlay. The companion can set the correct input when a service starts. Deferred until the mechanism is confirmed via protocol analysis. |
| Lighting control | Hardware-unique but not core to the WiFi speaker goal. Post-v1.0. |
| Microphone / karaoke | Out of scope for a WiFi speaker. Post-v1.0. |
| EQ / sound modes | Post-v1.0. |
| MQTT | REST + WebSocket covers all use cases. MQTT adds broker dependency for no v1.0 gain. |
| Native HA custom component | HA works fine as an HTTP client. A custom component is an optimisation, not a requirement. |
| Multi-device management | Auracast is hardware-level. One daemon, one master device. |
| Hotspot / captive-portal WiFi onboarding | The Pi-creates-its-own-network first-boot pattern. Post-v1.0; SD card WiFi config is sufficient for v1.0. |
