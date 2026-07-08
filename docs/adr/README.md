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
| [015](015-bluetooth-control-transport.md) | Bluetooth Control Transport is BLE GATT (via bleak) | Accepted |
| [016](016-companion-owns-spotify-lifecycle.md) | Companion Owns the Spotify Connect Lifecycle | Accepted |
| [017](017-runtime-layout.md) | Runtime Layout | Accepted |
| [018](018-systemd-service.md) | systemd Service Model | Accepted |
| [019](019-distribution-approach.md) | Distribution Approach — Image Pipeline | Accepted |
| [020](020-appliance-hardening.md) | Appliance Hardening | Accepted |
| [021](021-network-provisioning.md) | Network Provisioning Architecture | Accepted |
| [022](022-volume-authority.md) | Volume Authority Model | Accepted |
| [023](023-hci-controller-reset-on-startup.md) | HCI Controller Reset at Startup via hciconfig | Accepted |
| [024](024-task-supervision.md) | Task Supervision and the Two-Tier Recovery Model | Accepted |
| [025](025-appliance-naming-model.md) | Appliance Naming Model — Single Identity with Optional Service Overrides | Accepted |
| [026](026-bluetooth-audio-pairing.md) | Bluetooth Classic A2DP Pairing and Audio Readiness | Accepted |
| [027](027-bluetooth-bonding-architecture.md) | Bluetooth Bonding Architecture — D-Bus Discovery, Scoped Bondable Mode, Immediate Pairing | Accepted |
| [028](028-audio-readiness-model.md) | Audio Readiness Model and A2DP Connection Management | Accepted |
| [029](029-python-3-14-standardization.md) | Python 3.14 Standardization | Accepted |
| [030](030-bluez-gatt-configuration.md) | BlueZ GATT Configuration: Disable EATT, Enable AutoEnable | Accepted |
| [031](031-factory-reset-contract.md) | Factory Reset Contract | Accepted |
| [032](032-capability-probing.md) | Detect Optional Capabilities by Probing the Vendor Protocol | Accepted |
| [033](033-speaker-standby-detection.md) | Speaker Power State — Standby Detection via Control-Plane Liveness Probing | Accepted |
| [034](034-power-command-reconnect-wait.md) | Wait for Reconnect Across a Power Command's Own BLE Drop | Accepted |
| [035](035-state-ownership-and-signal-pipeline.md) | State Ownership and the Signal → Scene Pipeline | Accepted |
