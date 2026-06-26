# Architecture Decision Records

This directory contains Architecture Decision Records (ADRs) for partybox-companion.

An ADR documents a significant architectural decision: the context that led to it, what was decided, and the trade-offs accepted. Future contributors can understand not just what the architecture is, but why.

New ADRs should be added when a significant design decision is made. Superseded decisions should be marked with a status of **Superseded by ADR-NNN** rather than deleted.

## Index

| ADR | Title | Status |
|---|---|---|
| [001](001-project-vision.md) | Project Vision: Appliance over Library | Accepted |
| [002](002-monorepo.md) | Monorepo with uv Workspace | Accepted |
| [003](003-sdk-first.md) | SDK-First Architecture | Accepted |
| [004](004-daemon.md) | Headless Daemon (partyboxd) | Accepted |
| [005](005-appliance.md) | Appliance Layer (companion) | Accepted |
| [006](006-capability-model.md) | Capability-Based Model Support | Accepted |
| [007](007-tcp-only.md) | HTTP over TCP Only (No Unix Domain Sockets) | Accepted |
| [008](008-mqtt-deferred.md) | MQTT Deferred to Post-v1.0 | Accepted |
| [009](009-single-device.md) | Single-Device Assumption | Accepted |
| [010](010-sdk-scope.md) | SDK Scope — Hardware-Unique Features Only | Accepted |
| [011](011-companion-portal.md) | Companion Portal — Product Name and Scope | Accepted |
| [012](012-interoperability-positioning.md) | Interoperability Positioning and Legal Hygiene | Accepted |
| [013](013-user-journey-milestone-ordering.md) | Milestone Ordering — User Journey First | Accepted |
| [014](014-audio-transport-viability.md) | Audio Transport Viability Spike | Accepted |
