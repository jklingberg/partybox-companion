# Roadmap

## Current status

**M1 — Foundation**, **M2 — Bluetooth Transport**, and **M3 — Audio Transport Viability** are complete.

**M4 (Protocol Foundation) and M5 (Core Device Capabilities) are complete** — delivered as a single milestone. The `partybox` SDK now has a typed protocol layer (frame codec, message dataclasses, encode/decode), `PartyBoxDevice` with three capabilities, and a clean public API verified end-to-end on a real PartyBox 520:

- `await speaker.power.turn_on()` / `turn_off()` — confirmed opcode `AA 03 01 05/04`
- `await speaker.device_info.manufacturer()` → `"JBL"`
- `await speaker.device_info.firmware_version()` → `"26.2.10"` — confirmed opcode `AA 21 00` on hardware
- `speaker.battery` → `None` on the mains-powered 520; `BatteryCapability` present on portable models
- `device.events()` is deferred to **M6** (daemon integration)

Protocol work also confirmed: the excelpoint vendor protocol uses `AA [opcode] [length] [payload]` with no checksum; state notifications are opcode-`0x12` TLV packets; a `_drain_inbox_sentinels` fix in `BleakTransport` resolves spurious BlueZ disconnect callbacks during RPA resolution.

One intentional gap: `device_info.model()` and `serial_number()` raise `NotImplementedError` — the model/serial string appears only in the power-off TLV state dump (tag `0x40`) and no direct request opcode was found despite systematic probing. Documented in `open-questions.md`; the xfail hardware test tracks it.

**M6 — Daemon** and **M7 — REST API** are complete. Work proceeds to **M8 — Companion Portal MVP**.

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

### M3 — Audio Transport Viability ✅

A technical viability spike answering the most critical architectural question before any higher-level features are built:

> *Can a Raspberry Pi reliably stream audio to a JBL PartyBox over Bluetooth A2DP while simultaneously maintaining the BLE control connection?*

**Verdict: viable.** Validated on a real PartyBox 520 from the Pi. The output is evidence, not production code — the exploratory toolkit lives in [`spike/m3-audio/`](../spike/m3-audio/) and the full writeup is [docs/validation/m3-findings.md](validation/m3-findings.md).

To keep the spike minimal we validated with **local audio first** (a generated tone via PipeWire) rather than coupling the experiment to librespot; routing a real Spotify Connect stream is the production path and lands in M9.

**Validated:**

- BlueZ pairs/bonds with the PartyBox as an A2DP sink; PipeWire routes to it (codec **SBC**)
- Audio is clean — **zero xruns**, no disconnects across every sample
- The BLE control connection coexists with active A2DP (commands round-tripped mid-stream)
- A2DP reconnect is reliable and fast — **10/10 cycles, ~1.2 s median**

**Discovered (deferred to M6 — daemon, not blockers):** naive per-session BLE connection management is fragile — the Pi's BlueZ/controller wedges under connect-churn against the speaker's rotating LE addresses (recoverable with a controller reset). Reliable BLE control needs an LE bond + connection management. Also deferred: the formal 30-min extended run, librespot/Spotify routing (M9), and standby-mode reconnect.

**If this milestone had failed:** the appliance architecture would have been reconsidered before further investment. It did not — work proceeds to M4.

---

### M4 — Protocol Foundation + Core Device Capabilities ✅

**Package:** `partybox`

> M4 (Protocol Foundation) and M5 (Core Device Capabilities) were delivered together.

Frame codec, message dataclasses, encoder and decoder. `PartyBoxDevice` with capability API, `Scanner` top-level entry point. Capability coverage:

| Capability | Always present | Status |
|---|---|---|
| `PowerCapability` | yes | ✅ confirmed opcodes `AA 03 01 05/04` |
| `DeviceInfoCapability` | yes | ✅ `manufacturer()` + `firmware_version()` (opcode `AA 21 00`); `model()` / `serial_number()` deferred (opcode not found) |
| `BatteryCapability` | no (portable models only) | ✅ implemented; `None` on mains-powered 520 |

**Protocol layer:** stateless and pure — no I/O, no async. Every message type is a frozen dataclass. Bytes confirmed from real hardware captures serve as test fixtures so CI runs without hardware.

**BleakTransport fix:** `_drain_inbox_sentinels` resolves spurious BlueZ disconnect callbacks that fire during `client.connect()` while resolving rotating private addresses.

**Known gap:** `device_info.model()` and `serial_number()` raise `NotImplementedError`. The model/serial string (tag `0x40`) appears only in the power-off TLV state dump; no direct request opcode was found despite systematic probing of 50+ opcodes. Tracked in `open-questions.md`; hardware test is `xfail(strict=True)`.

**Event stream** (`device.events()`) deferred to M6 (daemon integration).

**Done:** power on/off, firmware version, and battery presence detection work end-to-end against a real PartyBox 520. 68 unit tests pass in CI; 8 hardware tests pass / 1 xfailed on the Pi.

---

### M5 — Core Device Capabilities ✅

Delivered as part of M4 — see above.

---

### M6 — Daemon ✅

**Package:** `partyboxd`

Daemon lifecycle, connection management, and a minimal HTTP API. The daemon owns the speaker connection — scanning, connecting, maintaining, and reconnecting — and exposes current state over HTTP.

**Done when:** `partyboxd` starts, connects to a real PartyBox, maintains the connection, and serves `GET /api/v1/status` returning connection status, firmware version, and battery level (if available) as JSON. Power state is intentionally absent — no confirmed query opcode exists. The `/status` endpoint is a minimal placeholder; the full REST API lands in M7.

**Validated on hardware (JBL PartyBox 520, 2026-06-27):**

- `partyboxd` starts, connects in ~12 s, and serves `GET /api/v1/status`
- `connected: true`, `address` populated, `battery: null` (mains-powered, correct)
- Graceful SIGTERM shutdown confirmed
- `firmware: null` — the `AA 21 00` opcode no longer elicits `AA 22` on this unit; the speaker replies instead with `AA 12 04 00 53 01 00` (state dump, opcode `0x12`). Cause unknown — possible protocol behaviour change; tracked in `open-questions.md`. The daemon degrades gracefully.

**SDK additions (partybox):** `PartyBoxDevice.address`, `PartyBoxDevice.drain_until_disconnect()`, and a fix to `connect()` to handle reconnection after `ConnectionLostError`.

---

### M7 — REST API ✅

**Package:** `partyboxd`

The REST API is the primary integration surface for all external clients — Companion Portal, CLI, Home Assistant, and future integrations. It exposes the daemon's domain model rather than SDK implementation details. Clients interact with concepts like speaker, battery, power, and firmware — not transports, opcodes, or Bluetooth.

**Endpoints:**

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/v1/health` | No | Daemon liveness; returns `status`, `version`, `speaker_connected` |
| `GET` | `/api/v1/speaker` | Yes | Speaker state: `connected`, `address`, `firmware`, `battery` |
| `GET` | `/api/v1/battery` | Yes | Battery level (0–100); 404 for mains-powered, 503 if disconnected |
| `POST` | `/api/v1/power/on` | Yes | Turn speaker on; 204 or 503 |
| `POST` | `/api/v1/power/off` | Yes | Turn speaker off; 204 or 503 |
| `WS` | `/api/v1/events` | `?api_key=` | Real-time event stream (`connected`, `disconnected`, `power_changed`) |

**Authentication:** optional API key via `X-Api-Key` header, configured with `PARTYBOXD_API__API_KEY`. Disabled by default. WebSocket clients supply the key as a `?api_key=` query parameter.

**Error responses** share a consistent shape: `{"detail": {"error": "<code>", "message": "<description>"}}`.

**Also delivered:**
- `DeviceManager.power_on()`, `power_off()`, `subscribe()`, `unsubscribe()`
- `EventBus` in `partyboxd.device.events` — fan-out dispatcher from manager to WebSocket clients
- `GET /api/v1/status` from M6 replaced by the cleaner `/health` + `/speaker` split
- Interactive docs at `/api/docs`; API reference at `docs/api/v1.md`

---

### M8 — Companion Portal MVP

**Package:** `companion`

The Portal is the primary onboarding surface. A user who has just booted the device should be able to verify their speaker connection and configure the appliance from a browser. See [ADR-011](adr/011-companion-portal.md) and [ADR-013](adr/013-user-journey-milestone-ordering.md).

The Portal is built against the M7 REST API. All endpoints it needs — health, speaker state, power control — are already in place.

**This milestone covers:**
- First-boot setup wizard: Bluetooth verification, speaker pairing, basic service configuration
- Status view: speaker connection, power state, battery level, firmware version, daemon health
- Basic configuration: device name, service preferences (written to config file)
- Spotify Connect and AirPlay sections present but showing "not yet active"

**Network prerequisite:** The Portal assumes the Pi already has network connectivity. For v1.0, WiFi is configured by writing credentials to the SD card before first boot — Raspberry Pi OS supports `wpa_supplicant.conf` on the boot partition, requiring only a file editor and no terminal on the Pi itself. A hotspot/captive-portal mode is post-v1.0.

The Companion Portal does **not** include media playback controls.

**Done when:** A user who has booted the device (with network reachable) can verify speaker status, control power, and complete initial configuration from a browser without touching a terminal on the Pi.

---

### M9 — CLI

**Package:** `companion`

The `partybox` CLI binary. A command-line interface for users who prefer the terminal over the Portal, and a natural integration surface for scripting and automation.

```
partybox status           # speaker state: connection, firmware, battery
partybox power on/off     # send power commands
partybox watch            # stream live device events (WebSocket)
```

All commands operate against the running daemon via the M7 REST API.

**Done when:** CLI commands work end-to-end against a running daemon; `partybox watch` streams events until interrupted.

---

### M10 — Spotify Connect

**Package:** `companion`

librespot subprocess manager. Start on boot, restart on crash, stop on Bluetooth disconnect. The Portal's Spotify section (introduced in M8 as a placeholder) becomes active.

**Done when:** A Spotify client sees the PartyBox as a Connect device; playback starts and stops correctly; the daemon event stream reflects playback state; Portal shows Spotify as active.

---

### M11 — AirPlay

**Package:** `companion`

shairport-sync subprocess manager. The Portal's AirPlay section (introduced in M8 as a placeholder) becomes active.

**Done when:** An Apple device sees the PartyBox as an AirPlay receiver; Portal shows AirPlay as active.

---

### M12 — Companion Portal: Complete

**Package:** `companion`

The Portal is completed with full Spotify/AirPlay configuration flows, diagnostics, and administration. This milestone closes the gap between the MVP introduced in M8 and the full appliance experience.

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
