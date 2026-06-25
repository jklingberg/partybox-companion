# Companion Portal

The Companion Portal is the administrative interface for the partybox-companion appliance. It is served by the `companion` package as static files at `http://partybox.local:8080`.

Accessible at `http://partybox.local:8080` (or `http://<device-ip>:8080`).

## Status

Work in progress. Framework and tooling TBD.

## Requirements

- Communicates with `partyboxd` via REST API and WebSocket
- Served as static files by the `companion` package
- Build output: `packages/companion/src/companion/webui/static/`

## Scope

The Portal focuses on configuration, status, and diagnostics. See [ADR-011](../docs/adr/011-companion-portal.md) for the full scope definition.

The Portal does **not** include media playback controls (play, pause, skip, volume, queue). Those belong in Spotify, Apple Music, and AVRCP clients.

## Pages (planned)

### Setup (first-boot wizard)
- WiFi configuration
- Bluetooth verification and speaker pairing
- Spotify Connect configuration
- AirPlay configuration

### Status
- Connected speaker, power state, Bluetooth status
- Battery level and charging status
- Active streaming service (Spotify Connect / AirPlay)
- Daemon health

### Configuration
- Service settings (librespot, shairport-sync)
- Network settings
- Update channel
- Device preferences

### Diagnostics
- Service status
- Connection history
- Bluetooth diagnostics
- Log download
- Debug bundle generation
