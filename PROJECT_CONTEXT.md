# Project Context

## Vision

PartyBox Companion turns a JBL PartyBox into a polished WiFi speaker appliance.

The end-user experience should feel like a commercial consumer product:
- Flash an SD card
- Power on the Raspberry Pi
- Connect the speaker once
- Stream using Spotify Connect (and later AirPlay)
- No terminal, SSH, or Linux knowledge required

The Raspberry Pi becomes part of the speaker, not a general-purpose computer.

---

## Product Philosophy

This is an appliance, not a framework.

The goal is to build the best possible single-purpose product rather than a reusable platform.

Optimise for:

- simplicity
- reliability
- maintainability
- predictable behaviour
- excellent first-time user experience

Avoid adding abstractions before they are needed.

---

## Architecture

The project consists of three packages.

### partybox

Hardware SDK.

Responsible only for communicating with the speaker.

Must never know about Spotify, AirPlay, REST APIs, or the Portal.

---

### partyboxd

Owns the speaker connection.

Responsible for:

- Device lifecycle
- Connection management
- REST API
- Device event bus

Knows about the hardware, but not higher-level application services.

---

### companion

Owns the appliance.

Responsible for:

- Spotify Connect
- AirPlay (future)
- Portal
- Configuration
- Network provisioning
- Audio lifecycle
- Service orchestration

This package is allowed to coordinate multiple services.

---

## Development Philosophy

Architecture comes before implementation.

Every significant design decision should be documented in an ADR.

When uncertain:

- prefer simpler designs
- prefer explicit code
- avoid premature abstractions

Hardware validation is considered the source of truth.

The implementation should follow the hardware, not assumptions.

---

## Code Quality

The project uses:

- mypy --strict
- ruff
- pytest
- async/await throughout
- dependency injection where appropriate

New code should match the existing style.

---

## Milestones

Development is milestone-driven.

The roadmap is the source of truth.

Avoid implementing work that belongs to future milestones.

---

## Current Status

M1–M16 are complete.

Current focus is M17 (Reliability).

Completed:

- M17.1 — Supervisor
- M17.2 — Honest health model

Current work:

- M17.3 — Spotify lifecycle
- M17.4+ — Reliability improvements

---

## Design Filter

Before adding any feature, ask:

> Does this make the PartyBox a better WiFi speaker in a way that Spotify Connect, AirPlay, or Bluetooth AVRCP cannot?

If the answer is "no", it probably does not belong in the project.

---

## Assistant Expectations

When helping with this project:

- Review architecture critically.
- Prefer simple solutions.
- Point out unnecessary complexity.
- Consider long-term maintainability.
- Keep package boundaries clean.
- Avoid introducing generic frameworks.
- Suggest ADR updates when architectural decisions change.
- Assume hardware validation is required before considering Bluetooth-related work complete.
