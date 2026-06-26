# ADR-013: Milestone Ordering — User Journey First

**Status:** Accepted

---

## Context

The original milestone ordering optimised for implementation complexity: SDK first, then daemon, then CLI, then REST API, then streaming services, and finally the Companion Portal at M10.

This ordering is natural from an engineering perspective — each milestone builds cleanly on the one before it. But it produces a product that is terminal-only for nine milestones and only becomes accessible to non-technical users at the last step before v1.0.

The problem becomes clear when you trace the intended v1.0 user journey:

1. Flash an SD card.
2. Boot the device.
3. Open `http://partybox.local`.
4. Complete setup in the Companion Portal.
5. Start streaming.

In the original ordering, step 3 would not work until M10. A user attempting this journey at M8 (after Spotify Connect was available) would find no Portal at all — just a daemon API with no browser interface. The streaming services would be running, but there would be no way to configure them without a terminal.

The Portal is not an optional feature that adds polish after the real work is done. It is the primary onboarding surface. If a user cannot verify their speaker connection or configure a service from a browser, the appliance goal is not met.

## Decision

**Principle:** features appear in the milestone where they first become necessary for the user journey.

Applied to the roadmap:

### M7 — Companion Portal MVP (moved from M11)

The Portal is introduced as M7 — immediately after the daemon — because it is the primary onboarding surface, not a finishing touch.

M6 already exposes a minimal status endpoint (`GET /api/v1/status`). The Portal MVP is built against that endpoint and does not depend on the full REST API. This is the earliest a browser interface is meaningful (the daemon must exist) and the correct position given that the user journey starts at the browser, not the terminal.

The M7 Portal covers the minimum needed to onboard a user:
- First-boot setup wizard (Bluetooth verification, speaker pairing, basic configuration)
- Status view (speaker connection, power, battery, firmware, daemon health)
- Basic configuration (device name, service preferences)
- Spotify Connect and AirPlay sections as placeholders ("not yet active")

The MVP is intentionally read-heavy. Control actions (power on/off from the browser) require the full REST API, which arrives in M8. This is an acceptable constraint for an early milestone.

The placeholders establish the Portal's structure before the services exist and activate automatically when M9 and M10 land — no additional Portal work is needed at those milestones.

### M8 — REST API & CLI (merged, follows Portal MVP)

The full REST API and CLI are developed together in M8. The CLI is a REST API consumer; developing them in the same milestone ensures the REST API is exercised immediately.

The Portal's control actions (power on/off from the browser) also become available once M8 is complete.

### M9, M10 — Spotify Connect and AirPlay (unchanged relative position)

No change in relative position. The Portal placeholder sections from M7 activate when these milestones complete.

### M11 — Companion Portal: Complete (split from old M11)

The Portal is completed after streaming services exist. This milestone adds Spotify/AirPlay configuration flows, full diagnostics, and log download — things that only make sense once the services are running.

Splitting the Portal into M7 (MVP) and M11 (Complete) rather than a single milestone was the key design choice. A single Portal milestone at M11 would have left a terminal-only product for four milestones after streaming was working. A single milestone at M7 would have required implementing Spotify/AirPlay configuration before those services existed.

### v1.0 — User outcome, not feature checklist

v1.0 is defined by a user outcome:

> A non-technical user can flash an SD card, boot the device, complete the initial setup in the Companion Portal, and start streaming music via Spotify Connect or AirPlay — without ever opening a terminal.

A feature checklist describes what was built. A user outcome describes whether it works as a product. The checklist (systemd units, mDNS, SD card image) follows from the outcome; the outcome drives what belongs on the checklist.

## Consequences

**Benefits:**
- The Portal is present from M7 onwards — before streaming services, before the full REST API. The onboarding path exists at the earliest viable point.
- The v1.0 outcome is testable: hand the image to a non-technical user and observe whether they can complete setup without a terminal.
- The Portal MVP builds against the M6 status endpoint, which already exists. No new backend work is required to unblock the Portal.
- Merging CLI and REST API into M8 removes an ordering anomaly (CLI before its own API) without delaying the Portal.
- The Portal placeholder pattern means M9 and M10 do not need additional frontend work — the Portal structure is already in place.

**Accepted trade-offs:**
- The M7 Portal is read-heavy: it can show status but cannot control the speaker (power on/off) until M8's full REST API lands. This is an intentional scope boundary for the MVP, not a gap.
- The WiFi prerequisite is not solved in-Portal. The Portal assumes network connectivity. For v1.0, this is addressed by writing WiFi credentials to the SD card before first boot (Raspberry Pi OS boot-partition `wpa_supplicant.conf`). A hotspot/captive-portal mode is post-v1.0. This is a real constraint for users who cannot edit the SD card (e.g. corporate-managed machines) — accepted as a v1.0 scope boundary.
- Splitting the Portal into M7 (MVP) and M11 (Complete) means Portal work is distributed across the milestone sequence. Contributors need to know which milestone their Portal work targets.

**Relationship to other decisions:**
- See [ADR-011](011-companion-portal.md) for the Portal's scope and the rationale for its name.
- See [ADR-001](001-project-vision.md) for the appliance-over-library vision that this ordering serves.
