# ADR-014: Audio Transport Viability Spike

**Status:** Accepted

---

## Context

The partybox-companion appliance depends on a Raspberry Pi simultaneously maintaining two Bluetooth connections to the PartyBox:

1. **BLE control connection** — the custom RFCOMM protocol for device control (power, battery, device info)
2. **A2DP audio connection** — standard Bluetooth audio profile for streaming music from librespot

Neither of these connections has been validated on a Raspberry Pi. The architecture assumes they can coexist. If they cannot — or if the audio quality is insufficient, or if long-running sessions produce dropouts — the entire appliance concept is in question.

The original milestone ordering (M2 → M3 Protocol Foundation → M4 Core Capabilities → M5 Daemon → M6 Daemon Portal → ...) deferred any validation of this assumption until M8 (Spotify Connect), by which point significant protocol, daemon, and Portal work would already be complete.

This creates substantial schedule risk: months of implementation could be invalidated by a hardware or BlueZ compatibility problem that could have been discovered in days.

## Decision

Insert a technical viability spike as **M3 — Audio Transport Viability**, immediately after the Bluetooth transport foundation (M2) and before the protocol and daemon layers (M4+).

The purpose of M3 is to answer one question:

> *Can a Raspberry Pi reliably stream music to a JBL PartyBox over Bluetooth A2DP while simultaneously maintaining the BLE control connection?*

This is a validation milestone, not an architecture milestone. The output is evidence — a working end-to-end audio session — not production code. No daemon abstractions, no REST API, no configuration system. The code produced in M3 is exploratory; it does not need to survive into later milestones.

M3 is explicitly risk-ordered, not feature-ordered. It deliberately interrupts the "clean dependency graph" of the implementation milestones because validating the core assumption is more valuable than maintaining sequence tidiness.

## Consequences

**Benefits:**
- The largest architectural assumption is validated before any higher-level investment is made.
- If M3 succeeds, all subsequent work proceeds with confidence that the audio delivery model is sound.
- If M3 fails, the failure is discovered with M2's foundation as the only sunk cost — before Protocol Foundation, daemon, Portal, or streaming services are built.
- The M3 spike produces concrete, hardware-validated knowledge about BlueZ A2DP configuration, PipeWire routing, and coexistence behaviour that directly informs the M9 (Spotify Connect) implementation.

**Accepted trade-offs:**
- All subsequent milestones shift from their previously numbered positions (M3→M4, M4→M5, M5→M6, M6→M7, M7→M8, M8→M9, M9→M10, M10→M11). Documentation and ADR cross-references must track this shift.
- M3 is a spike: the code is exploratory and not expected to be production-quality. Contributors should not treat M3 outputs as the canonical implementation of audio routing — that comes in M9.
- The "clean dependency graph" of the original milestone sequence is interrupted. This is intentional: risk reduction takes priority over implementation order cleanliness.

**If M3 fails:** The project must reconsider the core appliance architecture before investing further. Possible alternatives include a USB audio bridge, a different Pi model, an A2DP stack that is not BlueZ, or a fundamentally different audio delivery path. This ADR does not prejudge those alternatives — it only ensures the failure is discovered early.

**Relationship to other decisions:**
- See [ADR-001](001-project-vision.md) for the appliance vision that M3 validates.
- See [ADR-013](013-user-journey-milestone-ordering.md) for the user-journey milestone ordering principle that governs subsequent milestones.
