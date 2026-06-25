# ADR-011: Companion Portal — Product Name and Scope

**Status:** Accepted

---

## Context

The `companion` package includes a browser-based interface served at `http://partybox.local`. In early documentation, this interface was referred to as the "Web UI."

That label is technically accurate but product-level misleading. "Web UI" implies a browser-based remote control — the kind of interface where a user goes to play music, skip tracks, or adjust volume. That is explicitly not what this interface is for.

The appliance delegates media playback entirely to Spotify Connect (via librespot) and AirPlay (via shairport-sync). Users control playback from Spotify, Apple Music, or any AVRCP-capable client. The browser interface has no role in that layer.

Calling it a "Web UI" also understates its importance. For a non-technical user, the browser interface is the primary interaction surface for the entire appliance — setup, configuration, monitoring, and diagnostics. It belongs to the control plane, not the media plane.

## Decision

The user-facing product name for the browser interface is **Companion Portal**.

This name change is a product-level documentation and architecture decision. It does not affect Python package names (`companion`), module paths (`webui/`), or implementation identifiers. Code can continue to use `webui` where appropriate.

### Responsibilities of the Companion Portal

**Setup** — the Portal is the first thing a user sees after flashing the SD card:
- WiFi configuration
- Bluetooth verification
- Speaker pairing
- Spotify Connect configuration
- AirPlay configuration

**Configuration** — ongoing management of the runtime:
- Bluetooth address and pairing
- Service settings (librespot, shairport-sync)
- Network configuration
- Update channel
- Device preferences and logging

**Status** — the current state of the appliance at a glance:
- Connected speaker, Bluetooth status
- Battery level and charging status
- Firmware version
- Active streaming service (Spotify Connect, AirPlay)
- Daemon health

**Diagnostics** — tools for troubleshooting:
- Service status
- Connection history
- Bluetooth diagnostics
- Log download
- Debug bundle generation

### Explicitly out of scope

The Companion Portal does not implement media playback controls:

| Excluded | Reason |
|---|---|
| Play / pause / skip | Spotify Connect and AVRCP handle this better |
| Volume control | librespot and shairport-sync manage volume within their protocols |
| Playlist browsing | Users browse playlists in Spotify or Apple Music |
| Playback queue | Same |
| Now-playing display | Nice to have, but not a first-class Portal responsibility |

Implementing any of these in the Portal would duplicate controls that already exist in purpose-built clients. The filter question: *does this make the PartyBox a better WiFi speaker in a way that Spotify Connect, AirPlay, or AVRCP cannot?* For media playback controls, the answer is no.

## Consequences

**Benefits:**
- The name communicates purpose immediately. "Companion Portal" positions the interface as the administration surface for the appliance — not an alternative Spotify client.
- The scope constraint keeps the Portal focused and shippable. Implementing media playback would require deep integration with librespot and shairport-sync state, substantially increasing implementation complexity.
- Users who want playback controls use the right tool for the job — Spotify or Apple Music — rather than a lesser browser imitation.

**Accepted trade-offs:**
- Users accustomed to media-player web UIs may initially expect playback controls. The setup and status views are the primary value; that expectation is managed by the Portal's design.

**Relationship to other decisions:**
- See [ADR-005](005-appliance.md) for how the Portal is served within the companion appliance process.
- See [ADR-010](010-sdk-scope.md) for the same "hardware-unique only" scope rule applied to the SDK.
