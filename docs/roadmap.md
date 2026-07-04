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

**M6 — Daemon**, **M7 — REST API**, **M8 — Companion Portal MVP**, **M9 — Spotify Connect**, **M10 — Portal UX**, **M11 — Companion Portal: Complete**, **M12 — Appliance Runtime**, and **M13 — Distribution & Packaging** are complete.

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

The REST API is the primary integration surface for all external clients — Companion Portal, Home Assistant, scripts, and future integrations. It exposes the daemon's domain model rather than SDK implementation details. Clients interact with concepts like speaker, battery, power, and firmware — not transports, opcodes, or Bluetooth.

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

### M8 — Companion Portal MVP ✅

**Package:** `companion`

The Portal is the primary onboarding surface. A user who has just booted the device should be able to verify their speaker connection and configure the appliance from a browser. See [ADR-011](adr/011-companion-portal.md) and [ADR-013](adr/013-user-journey-milestone-ordering.md).

**Implemented:**
- `CompanionSettings` — `COMPANION_*` env vars (host, port, data\_dir); defaults to `0.0.0.0:8080`
- `make_portal_router()` — extends the partyboxd FastAPI app in-process with two new endpoints and the Portal HTML
- `GET /api/v1/config` / `PUT /api/v1/config` — persistent appliance config (`device_name`, `setup_complete`) stored as JSON; public (no auth required)
- `GET /` — serves the single-page Portal HTML (self-contained; no external CDN, no build step)
- `companion/__main__.py` — full appliance entry point: creates DeviceManager + daemon app + Portal router, runs uvicorn
- **Portal features:** first-boot setup wizard (2-step: name + connection check), status dashboard (speaker card, system card), power controls (Turn On / Turn Off), real-time WebSocket live updates, Spotify Connect + AirPlay placeholder sections, settings panel, API key modal, toast notifications, mock mode (`?mock` URL param for UI development without a daemon)

**Design:** dark appliance theme, system font, zero external dependencies, responsive (works on mobile), ARIA landmarks and live regions for accessibility.

**Network prerequisite:** The Portal assumes the Pi already has network connectivity. For M8, WiFi is configured by writing credentials to the SD card before first boot. Zero-touch WiFi provisioning (captive AP + portal) lands in M14.

The Companion Portal does **not** include media playback controls.

**Done:** 16 unit tests pass in CI; mypy strict passes; Portal runs without hardware using `?mock`.

---

### M9 — Spotify Connect ✅

**Package:** `companion`

librespot subprocess manager. Start on boot, restart on crash, clean shutdown. The Portal's Spotify section (introduced in M8 as a placeholder) becomes a live status card.

**Implemented:**
- `SpotifySettings` — `COMPANION_SPOTIFY__*` env vars (`connect_name`, `bitrate`, `backend`); sensible defaults
- `SpotifyService` — manages the librespot subprocess: starts it, monitors stderr for playback state, restarts after unexpected exits, terminates cleanly on shutdown; degrades gracefully when librespot is not installed
- `GET /api/v1/spotify` — public endpoint returning `{running, active, device_name}`; no auth required (status only, no sensitive data)
- Appliance entry point updated to run `SpotifyService` as a task alongside `DeviceManager`
- Portal Spotify card now shows service status (running/stopped), device name, and playback state; polls every 15 s; works in `?mock` mode

**Design intent:** librespot is an implementation detail. The product is "this Pi appears as a Spotify Connect speaker." Playback control (volume, skip, queue) remains in Spotify clients. The Portal reports appliance state only. No generic service-manager abstractions were introduced.

**Distribution note:** During development the Raspotify Debian package is used as a convenient source of a prebuilt `librespot` binary. The `raspotify.service` systemd unit must be disabled — Companion is the sole orchestrator; a second service manager must not conflict. Before v1.0, librespot must ship as part of Companion so no manual installation step is required. See [ADR-016](adr/016-companion-owns-spotify-lifecycle.md).

**Done when:** A Spotify client sees the PartyBox as a Connect device; librespot is automatically managed by the daemon; unexpected exits are recovered; Portal correctly reflects service state.

---

### M10 — AirPlay *(post-v1.0)*

**Package:** `companion`

shairport-sync subprocess manager. The Portal's AirPlay section (introduced in M8 as a placeholder) becomes active.

Deferred to post-v1.0 to focus on a reliable Spotify Connect experience first. See the "Deliberately deferred" section.

**Done when:** An Apple device sees the PartyBox as an AirPlay receiver; Portal shows AirPlay as active.

---

### M11 — Companion Portal: Complete ✅

**Package:** `companion`

The Portal is completed with full Spotify configuration flows, diagnostics, and administration. This milestone closes the gap between the MVP introduced in M8 and the full appliance experience.

**Implemented:**
- `ConfigStore` — shared config storage (`data_dir/config.json`) with extended `PortalConfig`: `device_name`, `spotify_connect_name`, `spotify_bitrate`; replaces inline read/write in both routers
- **Spotify Connect configuration** — Settings panel now has a dedicated Spotify section: device name (what Spotify sees), and audio quality (96/160/320 kbps); changes persist across reboots
- `SpotifyService.update_settings()` — updates running settings and terminates the current librespot process so it restarts with the new config; no daemon restart needed
- `POST /api/v1/spotify/restart` — Portal calls this after saving config changes; reads current `PortalConfig` and applies it to the live service
- `GET /api/v1/debug/bundle` — downloadable ZIP (generated in-process, no shell calls) containing `version.json`, `config.json`, `services.json`, `system.json`; linked from the System card
- **Diagnostics section** — always-visible health summary below the dashboard grid: Speaker connection (with address), Spotify Connect status and device name, System version
- **Settings panel** — redesigned with two sections (Appliance / Spotify Connect); saves all fields in a single `PUT /api/v1/config`, then calls `/spotify/restart` only when Spotify settings changed
- **Boot from portal config** — `__main__.py` reads `PortalConfig` at startup so user-saved Spotify device name and bitrate survive appliance reboots, overriding env-var defaults
- Appliance name and Spotify device name are now separate, clearly labelled fields; the "rename" warning on the Spotify card points to the Settings section
- Restart button on Spotify card (visible when service is running)

**Done when:** A user can configure Spotify Connect device name and audio quality from the Portal; settings survive reboots; a debug bundle can be downloaded for support; all Portal sections show live status.

---

### M12 — Appliance Runtime ✅

**Package:** `companion`

Turn Companion from a development process into a proper Linux service. After M12, the appliance runs itself — no manual commands, no `nohup`, no open SSH session required.

**Implemented:**
- `system/systemd/companion.service` — production systemd unit: `Type=simple`, `Restart=on-failure`, `RestartSec=5`, `TimeoutStopSec=30`, `WantedBy=multi-user.target`, `After=network-online.target bluetooth.service`
- `system/systemd/companion.env` — env file template installed to `/etc/companion/companion.env`; documents all operator-tunable settings
- `StateDirectory=companion` — systemd creates and owns `/var/lib/companion/` before `ExecStart`; no setup scripts needed
- `AmbientCapabilities=CAP_NET_BIND_SERVICE` — `companion` user can bind port 80 without root; no reverse proxy needed
- `StandardOutput=journal` / `SyslogIdentifier=partybox-companion` — all output captured by journald; `journalctl -u partybox-companion` for log access
- Journald-aware logging format: timestamps omitted from Python log records when `JOURNAL_STREAM` is in the environment (journald provides them); full timestamps preserved in terminal
- `COMPANION_LOG_LEVEL` env var — configurable log level without code changes (default: `INFO`)
- Single `ConfigStore` instance — `make_portal_router` now accepts an injected store; eliminates three redundant `ConfigStore` instances that read/wrote the same file
- Debug bundle now includes recent journal log lines (`logs.txt`) via `journalctl --unit=partybox-companion`
- Developer handbook updated: `nohup`-based restart workflow replaced with `sudo systemctl restart partybox-companion`; logs section updated to use `journalctl`
- ADR-017 (runtime layout) and ADR-018 (systemd service model) written

**Architecture decisions:** See [ADR-017](adr/017-runtime-layout.md) and [ADR-018](adr/018-systemd-service.md).

**Done when:** A Raspberry Pi boots into a fully running Companion appliance without any manual intervention. `systemctl status companion` shows `active (running)`.

> **Note on hardware validation:** The service unit is complete and architecturally sound. End-to-end boot validation (`systemctl enable --now partybox-companion` → Portal accessible) will occur as part of M18 (Release Candidate) on a clean SD card flash. The unit cannot be exercised in the devcontainer (no systemd).

---

### M13 — Distribution & Packaging ✅

**Package:** `companion`

Ship a complete, self-contained appliance. After M13, a user can flash an SD card image and boot directly into Companion — no terminal, no manual software installation.

M13 is split into three phases:

---

#### M13.1 — Image Pipeline ✅

Establish the release engineering foundation: the pipeline that turns a git tag into a bootable appliance image.

**Delivered:**
- `image/install.sh` — appliance setup script; runs inside the Pi OS chroot during CI, or directly on a Pi for manual installs. Installs system packages, librespot (via raspotify), uv, the Companion Python venv, systemd service, Avahi, BlueZ config, and WiFi power management.
- `image/config/` — host configuration files copied into the image by install.sh
- `.github/workflows/release.yml` — release pipeline triggered by `v*.*.*` tags: runs CI, builds the image with arm-runner-action (QEMU ARM64), compresses with xz, and publishes a draft GitHub Release
- `docs/adr/019-distribution-approach.md` — records the tool choices (arm-runner-action over pi-gen, raspotify as librespot source)

**Architecture decisions:** See [ADR-019](adr/019-distribution-approach.md).

**Done when:** `git tag v1.0.0 && git push origin v1.0.0` triggers a GitHub Actions run that produces `partybox-companion-v1.0.0.img.xz` as a draft release artifact. A Pi flashed with that image boots into a running Companion service accessible at `http://partybox.local`.

---

#### M13.2 — Image Polish ✅

**Delivered:**
- `image/config/base-image.env` — Pi OS base image pinned to a specific dated release; upgrades are a single-file diff
- `image/smoke-test.sh` — release gate: starts Companion inside the QEMU chroot, calls `GET /api/v1/health`, and fails the workflow if the appliance does not respond
- Version flow: `git tag → hatch-vcs → importlib.metadata → REST API / Portal / MOTD` (see ADR-019 Version management section)
- uv version pinning documented with release engineering rationale
- Image cleanup: uv/pip caches, build logs, bash history removed before publishing

---

#### M13.3 — Appliance Hardening ✅

Harden the appliance for unattended operation.

**Delivered:**
- **SD card longevity:** swap removed (`dphys-swapfile` purged), `/tmp` mounted as tmpfs (64 MB cap), journald set to volatile storage (no SD card writes)
- **Service pruning:** `apt-daily.timer`, `apt-daily-upgrade.timer`, `unattended-upgrades`, `man-db.timer`, `triggerhappy`, `ModemManager` disabled — each with documented rationale
- **Headless boot:** `gpu_mem=16` frees ~60 MB GPU-reserved RAM; firmware and Plymouth splash screens removed; `quiet` retained on serial UART
- **ADR-020** — records all M13.3 decisions, including deferred items (hardware watchdog, `noatime`, Bluetooth plugin restrictions) and the intentionally open audio architecture question

**Done when:** An image built from a release tag runs unattended without unnecessary background activity, SD card wear, or splash screens. Future hardening opportunities are documented with clear validation requirements.

---

### M14 — Network Provisioning ✅

**Package:** `companion`

Remove the last piece of Raspberry Pi knowledge from the onboarding experience. After M14, a brand-new appliance can join a WiFi network without the user editing any files, connecting a keyboard, or opening a terminal.

The provisioning flow reuses the existing Portal and REST API, served during AP mode before the appliance has joined a network.

**Architecture decisions:** See [ADR-021](adr/021-network-provisioning.md).

**User flow:**

1. Flash SD card → insert → power on
2. If valid WiFi credentials exist: connect normally, no provisioning mode
3. Otherwise: create a temporary open access point — `PartyBox Companion Setup`
4. User connects with a phone or laptop (no password required)
5. The OS captive portal detection triggers automatically and opens a browser popup
6. The Portal's provisioning screen: scan for networks, select one, enter password
7. Appliance joins the selected network and tears down the temporary access point
8. Normal operation resumes — Portal accessible at `http://partybox.local`

**Architecture:**

- **AP mode via NetworkManager** — NM creates and manages the temporary access point natively (`802-11-wireless.mode ap`, `ipv4.method shared`); no separate hostapd required; NM's internal dnsmasq handles DHCP for AP clients automatically
- **Open AP** — the setup network has no WPA password; users connect without a passphrase; security relies on the local-only nature of the AP link, not WPA
- **Wildcard DNS via NM's internal dnsmasq** — a drop-in config in `/etc/NetworkManager/dnsmasq-shared.d/captive.conf` (`address=/#/<ap-ip>`) causes all DNS queries from AP clients to resolve to the appliance; iOS and Android use this to detect a captive portal and auto-open the browser popup; wildcard DNS is necessary — HTTP redirect alone is insufficient because OS probes are DNS-first
- **Captive probe interception with HTTP 302** — during provisioning mode, the Portal intercepts well-known captive portal probe paths (`/generate_204`, `/hotspot-detect.html`, `/connecttest.txt`, etc.) and returns HTTP 302 redirects to the Portal root; returning 204 would signal "no captive portal" to the OS and suppress the popup
- **HTTP on port 80** — iOS Captive Network Assistant renders pages only over HTTP on port 80; the Portal must be served on port 80 during AP mode; this brings port 80 binding into M14 (previously listed as an M16 goal); the `companion` user already has `CAP_NET_BIND_SERVICE` from M12
- **Hold AP until STA confirmed** — the access point is not torn down until NetworkManager confirms the new connection reached `ACTIVATED` state; if the connection fails or times out, the AP remains active so the user can retry
- **Reuse Portal and REST API** — provisioning is a new state of the existing Portal, not a separate web server; `ProvisioningService` in `companion/services/` manages the AP lifecycle; new `/api/v1/wifi/*` endpoints expose NM state and credential submission to the front end
- **nmcli subprocess** — the provisioning service interacts with NetworkManager via `nmcli` subprocess calls; no additional Python dependencies required

**New API surface:**

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/wifi/status` | Current WiFi state: `unprovisioned`, `ap_active`, `connecting`, `connected` |
| `GET` | `/api/v1/wifi/networks` | Scan result: available SSIDs with signal strength |
| `POST` | `/api/v1/wifi/connect` | Submit SSID + password; instructs NM to activate the connection |

**State machine:**

```
BOOT
  │
  ├─ NM has active WiFi STA? → Yes ──► NORMAL OPERATION
  │                             No  ──► PROVISIONING
  │
PROVISIONING: NM creates companion-ap (open, ipv4.method shared)
              dnsmasq wildcard DNS active
              Portal on port 80 with captive probe interception
  │
AP_ACTIVE → user connects → OS auto-opens browser popup
         → GET /api/v1/wifi/networks → selects SSID
         → POST /api/v1/wifi/connect
  │
CONNECTING: NM activates STA connection
         → poll for ACTIVATED; timeout → AP_ACTIVE (retry)
  │
CONNECTED: tear down companion-ap
         → NORMAL OPERATION (Portal at http://partybox.local)
```

**Done when:** A Pi flashed with the appliance image, with no WiFi credentials present, boots, creates a `PartyBox Companion Setup` access point, and a user can connect from a phone, open a browser without typing any URL, select their home network, enter a password, and have the Pi join that network and resume normal operation — no keyboard, monitor, terminal, or file editor required.

---

### M15 — Unified Volume Model ✅

**Packages:** `partybox`, `partyboxd`, `companion`

Companion exposes one logical speaker volume, regardless of which audio service is active. The user never thinks about Spotify volume, AirPlay volume, ALSA volume, PipeWire volume, or Bluetooth attenuation. There is simply: speaker volume.

**Design goal:** Companion becomes the owner of speaker state. Streaming services become producers of playback events rather than owners of hardware state. Spotify Connect, AirPlay, the Portal, the REST API, and any future integrations all operate on the same logical volume abstraction.

**SDK — `VolumeCapability`:**

```python
await speaker.volume.get()       # returns 0–100
await speaker.volume.set(percent)
```

The capability abstracts the underlying implementation. Clients never interact directly with BLE, librespot, or shairport-sync.

**Hardware authority:** The PartyBox is the authoritative source of hardware volume. If the user rotates the physical volume knob, Companion updates its internal state, the Portal reflects the change, and the REST API returns the new value. Companion never fights the hardware.

**Service integration:**

- *Spotify Connect* — initially librespot volume changes may continue to drive playback; once BLE volume commands are confirmed, Companion translates Spotify volume events into hardware volume changes instead.
- *AirPlay* — uses exactly the same `VolumeCapability`; no AirPlay-specific volume path exists.
- *Future services* — every future playback backend (Internet Radio, DLNA, etc.) integrates through the same capability.

**REST API:**

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/volume` | Current logical volume (0–100) |
| `POST` | `/api/v1/volume` | Set logical volume; body `{"level": <0–100>}` |

The API represents logical speaker volume and must not expose implementation details (BLE opcodes, ALSA mixer names, etc.).

**Portal:** A single volume control that targets the speaker, not the currently active streaming service.

**Implementation strategy:** This milestone is primarily architectural. The first implementation may continue using librespot's software volume if the BLE volume opcode is not yet confirmed. Once BLE volume commands are available, the backend switches transparently without changing any public APIs or client code.

**Done when:** Setting volume through any surface (Portal, REST API, physical knob) is reflected consistently across all others. No service owns volume independently.

---

### M16 — First Boot Experience ✅

**Package:** `companion`

Close the gap between development environment and polished appliance. After M16, a user who has provisioned WiFi (via M14) can reach the Portal from a browser with no manual configuration.

M14 gets the appliance onto the network and binds port 80. M16 validates the end-to-end experience once the appliance has joined a home network.

**Goals:**
- mDNS hostname: `http://partybox.local` resolves without any router configuration (Avahi already installed in M13.1; this milestone validates it end-to-end with port 80)
- Appliance identity: Pi hostname `partybox` confirmed working through a complete provisioning flow
- Sensible defaults: all settings work out of the box without user intervention
- Portal reachable at `http://partybox` once the router resolves the hostname

**Done when:** After WiFi provisioning, the Portal is reachable at `http://partybox.local` on port 80 with no terminal access required.

---

### M17 — Reliability

**Packages:** `companion`, `partyboxd`

Build confidence in the appliance's ability to run unattended. M17 introduces no new features — it ensures every existing feature survives real-world conditions without user intervention.

**Goals:**
- **Reboot recovery:** after a Pi reboot, Companion reconnects to the speaker and librespot re-registers with Spotify automatically ✓
- **Bluetooth recovery — speaker power cycle:** if the speaker is power-cycled or goes out of range, Companion reconnects when it returns ✓
- **Bluetooth recovery — controller wedge:** if the BT controller wedges, the daemon recovers without requiring `systemctl restart bluetooth` from the user — HCI reset in `ExecStartPre` at boot, plus (M18) an ATT-probe health check in `DeviceManager` that detects and recovers a zombie BLE connection after a `bluetoothd` restart within ~15 s ✓. The WirePlumber endpoint-degradation concern was resolved by the wireplumber 0.5.x pin (see M17.4 / ADR-028) and did not recur under M18 validation (9 h idle, zero `profile-unavailable`) ✓
- **Spotify visibility tied to speaker reachability:** librespot starts (and registers with Zeroconf) only after A2DP is confirmed available; it deregisters when A2DP has been unavailable long enough to indicate the speaker is off or out of range ✓
- **Crash recovery:** any component that exits unexpectedly is restarted with backoff; repeated failures surface in Portal diagnostics rather than being silently swallowed ✓
- **Extended run:** 30-minute streaming session (deferred from M3) validated with combined A2DP + BLE + librespot

**Validated (2026-07-02):** A2DP auto-connect is working end-to-end — speaker power-on via BLE GATT triggers A2DP connect within ~2 s; `audio_ready: true` is stable; music plays through the PartyBox 520 via Spotify Connect without manual intervention after reboot or speaker power cycle.

**Open items — all resolved under M18 validation (RC13, 2026-07-03):**
- ~~WirePlumber endpoint degradation after extended idle~~ — **resolved.** Reframed as a wireplumber-0.4.x/pipewire-1.x mismatch (see M17.4 / ADR-028) and fixed by the wireplumber 0.5.x pin. Did not recur at 10× the historical threshold: 9 h speaker-off idle plus a full day of connect/disconnect churn produced **zero `profile-unavailable`** occurrences. The "runtime WirePlumber recovery via sudo grant" once contemplated here was never needed and was not built; detection-only is the confirmed v1.0 posture.
- ~~30-minute extended streaming session~~ — **done.** STREAM-01 (30-min synthetic tone, 0 xruns, flat RSS) and STREAM-02 (100-min real Spotify Connect, 33 tracks, 0 xruns, 0 errors).

**Done when:** The appliance survives a reboot, a speaker power cycle, and a 30-minute streaming session without user intervention. **✅ Complete** — validated end-to-end in the RC13 run; see [runs/2026-07-02-rc13.md](validation/runs/2026-07-02-rc13.md).

---

### M18 — Appliance Validation & QA

**Packages:** `companion`, `partyboxd`

No new functionality. This milestone certifies the appliance against the
project's regression suite before the Release Candidate.

The suite itself lives in
[docs/validation/appliance-validation.md](validation/appliance-validation.md) —
a standalone specification (scenario catalog, expected behaviours, evidence
requirements, verdict rules) that evolves independently of the roadmap and is
re-run before every release. Each execution produces a dated run report under
[docs/validation/runs/](validation/runs/).

Validation is automated wherever possible: Claude drives the appliance
remotely (REST API, SSH, systemctl, journalctl, nmcli, wpctl, BLE power
control) and reserves human hands for physically unavoidable actions
(pairing-mode button, phone-based Bluetooth contention, range tests).

The goal is not to maximize PASS results — it is to surface behaviours that
surprise us, fix what blocks release, and document the rest.

**Done when:**

* Every scenario in the validation suite has been executed on real hardware
  (or explicitly deferred with rationale in the run report).
* All failures are fixed or waived with explicit rationale.
* Appliance logs contain no unexplained warnings or errors during normal
  operation (VAL-LOG scenarios pass).
* The appliance demonstrates stable unattended operation (soak scenarios).

**Status:** RC13 run essentially complete — see
[runs/2026-07-02-rc13.md](validation/runs/2026-07-02-rc13.md). Every
autonomously-runnable scenario has a verdict; release-blocking defects found
and fixed: fresh pairing, bluetoothd-restart zombie recovery, corrupt-config
crash loop, debug-bundle logs, plus incident fixes (librespot log surfacing,
playback-state detection) and one confirmed audio-UX defect deferred to M19
(INC-2: WirePlumber's 0.40 default sink volume ships music at 40 % — pin the
A2DP node to 100 % on connect).

**Remaining (both require physical access to the speaker):**
- **SPKR-06** — out-of-range walk and return.
- **FAULT-05** — stale-bond recovery (needs `bluetoothctl remove` + a
  pairing-mode button press; best run right before a deliberate re-pair,
  which also re-confirms BOOT-02's happy path).

Everything else — boot/reboot, speaker lifecycle, host lifecycle, Bluetooth
contention (BT-01/02/03), streaming (STREAM-01/02/03/04), fault injection
(FAULT-01/02/03/04/06), network, resources, soak (11.5 h continuous), log
quality, and the full REST/auth surface (API-01/02/03/04) — is validated.

---

### M19 — Release Candidate

No significant new functionality. This milestone verifies that all the pieces work together and that the project is ready to ship.

**Goals:**
- End-to-end validation on real hardware: flash a fresh SD card → boot → reach Portal → stream Spotify → reboot → stream again
- Fresh-pairing validation: exercise `bluez_dbus.py`'s `org.bluez.Agent1` flow end to end — `Pair()`, agent registration, first-time bonding — against a not-yet-bonded speaker under Python 3.14. This path has never been hardware-verified (see M16 implementation notes and [ADR-029](adr/029-python-3-14-standardization.md)); everything validated so far is reconnect against an already-bonded device, not first pairing.
- Documentation review: README, setup guide, and API reference are accurate and complete
- API freeze: no breaking changes to `/api/v1/*` after this point
- `CHANGELOG.md` drafted with user-visible changes since M6
- Version bumped to `1.0.0` in all packages
- Bug fixing only — no scope additions

**Done when:** Every M12–M17 milestone is complete. A clean flash-to-stream walkthrough succeeds without workarounds. The project is ready to tag.

---

### v1.0

Not a milestone. The point at which M12–M18 are complete and the release candidate is accepted.

```
git tag v1.0.0
```

**Known limitations at v1.0:**

- **BLE exclusive connection.** The daemon holds a persistent BLE GATT connection to the speaker. Because BLE GATT allows only one central at a time, third-party BLE clients — including the JBL app — cannot connect while the daemon is running. Workaround: stop `partybox-companion`, use the JBL app, then restart. An opportunistic connection model (connect to send a command, disconnect when idle) would allow coexistence but adds reconnect latency and state-management complexity; deferred to post-v1.0.
- **WiFi/Bluetooth coexistence on Pi 3 B+.** The BCM43438 chip shares the 2.4 GHz radio between WiFi and Bluetooth. During active A2DP audio streaming, Bluetooth timeslots can starve WiFi, causing mDNS (`partybox.local`) to become unreliable. Hostname resolution via the router's DNS (`partybox`) and direct IP remain unaffected. Mitigation applied: WiFi power management disabled (`wifi.powersave = 2`). Full resolution requires Ethernet or a dedicated USB WiFi adapter.

---

## Deliberately deferred

| Feature | Reason |
|---|---|
| AirPlay (M10) | Deferred to post-v1.0 to focus on a reliable Spotify Connect experience. shairport-sync subprocess manager follows the same pattern as librespot/SpotifyService. |
| SDK device events | `PartyBoxDevice` currently drains and discards unsolicited BLE notifications (see the drain loop in `device/partybox.py`). Exposing them as a typed async event iterable on the device — so the daemon can dispatch instead of discard — is the natural next step but not required for v1.0. Post-v1.0. |
| Third-party BLE client coexistence | An opportunistic BLE connection model (connect to send, disconnect when idle) would allow the JBL app and other BLE centrals to connect to the speaker while Companion is running. Deferred because it adds reconnect latency (~1–2 s per command), complicates connection-state management in `DeviceManager`, and is not required for the core appliance use case. The persistent connection is a conscious v1.0 trade-off. Post-v1.0. |
| Input source selection | Useful, but not needed to stream Spotify or AirPlay. The companion can set the correct input when a service starts. Deferred until the mechanism is confirmed via protocol analysis. |
| Lighting control | Hardware-unique but not core to the WiFi speaker goal. Post-v1.0. |
| Microphone / karaoke | Out of scope for a WiFi speaker. Post-v1.0. |
| EQ / sound modes | Post-v1.0. |
| MQTT | REST + WebSocket covers all use cases. MQTT adds broker dependency for no v1.0 gain. |
| Native HA custom component | HA works fine as an HTTP client. A custom component is an optimisation, not a requirement. |
| Multi-device management | Auracast is hardware-level. One daemon, one master device. |
| Bluetooth adapter reset from the Portal | Daemon-level Bluetooth recovery (handling a wedged controller without user intervention) is addressed in M17. A Portal-triggered "Reconnect" button backed by `POST /api/v1/bluetooth/reset` (requiring a `sudoers` entry for `systemctl restart bluetooth`) remains post-v1.0. |
