# ADR-009: Single-Device Assumption

**Status:** Accepted

---

## Context

JBL PartyBox speakers support Auracast for multi-room audio. Multiple speakers can be grouped together so they play synchronised audio. Should the daemon manage multiple speakers?

## Decision

Each daemon instance manages exactly one speaker. Auracast multi-room grouping is handled at the hardware level by the speaker firmware. The daemon communicates with the Auracast master device only.

## Consequences

**Benefits:**
- Significantly simpler state management. No device registry, no per-device routing, no group coordination.
- Connection management, event handling, and the HTTP API all assume one device. This is easier to reason about and test.
- Users who want multi-room audio run Auracast through the JBL hardware — the companion does not need to understand the topology.
- Multi-daemon setups (one Pi per speaker) are possible if needed, coordinated by the home automation layer above.

**Accepted trade-offs:**
- The daemon cannot command a group of speakers simultaneously (e.g. set volume on all Auracast members at once). Commands go to the master and the firmware propagates them to the group.
- The Companion Portal shows one speaker's status. Multi-speaker status aggregation would require a different architecture.

**Rejected alternative:** Multi-device daemon. Rejected because it multiplies the complexity of every component — the HTTP API needs per-device routing, the event bus needs device namespacing, state management becomes a collection rather than a single object — for a use case that is already well-served by the hardware.
