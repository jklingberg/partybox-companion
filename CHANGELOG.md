# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Everything built toward the first public release, **`v1.0.0`**. (Interim
release-candidate builds were tagged `v0.1.0-rc.1` … `v0.1.0-rc.13` during
development.) Milestone references in parentheses; see
[docs/roadmap.md](docs/roadmap.md) for the milestone narrative.

### Added

- **`partybox` SDK** — BLE GATT control transport via `bleak`, name-based
  scanner returning `PartyBoxDevice`, and a typed protocol codec
  (`encode`/`decode`) tested against real Bluetooth capture fixtures. Core
  capabilities: power, device info (firmware), battery (optional), and a
  volume placeholder pending opcode confirmation. (M2, M4)
- **Audio transport viability** validated on hardware — a single Pi can source
  A2DP audio to the speaker while maintaining a BLE control connection. (M3)
- **`partyboxd` daemon** — `DeviceManager` owning the BLE connection lifecycle
  (scan, connect, reconnect, health probe), a FastAPI REST API at `/api/v1/`,
  a WebSocket event stream, and optional `X-Api-Key` authentication. (M6, M7)
- **Companion appliance** — extends the daemon's FastAPI app in-process; the
  Companion Portal (single-page app) with status, configuration, diagnostics,
  and a downloadable debug bundle. (M8, M11, M12)
- **Spotify Connect** via a managed `librespot` subprocess, gated on audio
  readiness. (M9, M17.3)
- **Bluetooth A2DP pairing and bonding** over BlueZ D-Bus, with A2DP
  auto-connect and an honest health model (`ble_connected` + `audio_ready`).
  (M16, M17.2, M17.4)
- **Unified volume model** with source-tracked volume authority. (M15)
- **WiFi network provisioning** via a captive portal for first-boot setup. (M14)
- **Task supervision** with restart policies and per-task health tracking. (M17.1)
- **Distribution** — Raspberry Pi image build pipeline (`install.sh` +
  arm-runner), systemd service unit, and Avahi mDNS (`partybox.local`). (M13)
- **Appliance validation suite** and the RC13 hardware run report. (M18)
- Repository scaffold: workspace `pyproject.toml`, CI (lint, type-check, test),
  pre-commit hooks, contributing guide, and example scripts.

### Changed

- Standardized on Python 3.14 across all packages. (#36)
- Restricted Avahi to IPv4. (#38)

### Fixed

- Reset the HCI controller at startup and disable EATT to clear a wedged BlueZ
  state and the GATT "Unlikely Error". (#24, #25)
- Detect and recover a zombie BLE connection after a `bluetoothd` restart. (M18)
- Pin WirePlumber to 0.5.x and harden its restart to stop A2DP endpoint flap. (#35, #37)
- Prevent a `librespot` orphan process on companion restart. (#12)
- Survive a corrupted `config.json` instead of crash-looping. (M18)
- First-boot provisioning fixes: WiFi radio/polkit, network scan, and a false
  "connected" state on restart; hide the appliance's own AP from scan results. (#22, #23, #39)
