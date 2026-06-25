# ADR-001: Project Vision — Appliance over Library

**Status:** Accepted

---

## Context

JBL PartyBox speakers communicate over a Bluetooth protocol that is not publicly documented. An independent implementation of that protocol enables open integration with standard tools and ecosystems. There are several ways to package that work:

- A Python library for developers who want to control their speaker programmatically
- A Home Assistant custom component
- A standalone appliance that makes the speaker smart without requiring any code

The choice of primary framing affects every subsequent decision: API design, packaging, the install experience, what counts as "done", and who the project is for.

A library optimises for developer ergonomics. An appliance optimises for the end user who just wants the speaker to work better.

## Decision

The project is an **appliance**. The target user is someone who owns a JBL PartyBox and wants Spotify Connect, AirPlay, and browser-based control — without writing code, without a cloud account, and without relying on JBL's infrastructure.

The ideal experience:

1. Flash an SD card.
2. Configure WiFi.
3. Plug the Pi into the speaker.
4. Open `http://partybox.local`.
5. Done.

The Python SDK (`partybox`) and the REST API are deliverables alongside the appliance, not instead of it. They exist to make the appliance extensible, not to be the primary product.

## Consequences

**Accepted trade-offs:**
- The project scope is larger than a library alone. The Companion Portal, system integration, and packaging all become requirements.
- Appliance quality requires more polish: first-boot experience, mDNS, service management, update path.

**Benefits:**
- The project solves a complete problem for a concrete user, rather than providing infrastructure that others must build on.
- An appliance vision creates a clear quality bar. "Would a non-technical user be able to set this up?" is a testable question.
- The SDK and API remain useful to developers, but they are embedded in something that ships rather than left as building blocks waiting for someone to assemble.
