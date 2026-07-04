# ADR-004: Headless Daemon (partyboxd)

**Status:** Accepted

> **Historical note:** the CLI discussed in this ADR was ultimately removed before the first public release. The architectural reasoning remains part of the project's history.

---

## Context

The Bluetooth connection to a PartyBox must be maintained continuously. Multiple clients — the Companion Portal, a Home Assistant integration, a CLI, automation scripts — all need to interact with the same speaker simultaneously.

Without a daemon, each client would need to manage its own Bluetooth connection. Concurrent connections to the same RFCOMM channel may not be possible, and each client would need to implement connection management, reconnect logic, and state tracking independently.

## Decision

A persistent headless daemon (`partyboxd`) owns the Bluetooth connection and exposes a stable HTTP API. Clients connect to the daemon over HTTP; they never connect to Bluetooth directly.

Responsibilities:
- Own the Bluetooth connection for the lifetime of the daemon
- Manage reconnects transparently
- Maintain authoritative device state
- Expose REST endpoints and a WebSocket event stream
- Re-emit device events to all connected clients

The daemon is a separate binary (`partyboxd`) that can be installed and used without the full appliance. It is useful standalone for power users who want the API without the Companion Portal.

## Consequences

**Benefits:**
- Single Bluetooth connection shared across all clients.
- State is maintained in one place; no client needs to poll the hardware.
- Clients are simple HTTP consumers — no Bluetooth knowledge required.
- The daemon can be managed by systemd independently of the companion.

**Accepted trade-offs:**
- Two-process model during development (daemon + CLI/browser) rather than a single script.
- HTTP adds latency compared to direct Bluetooth access for scripting use cases.
- The daemon must be running before any client can function.

**Rejected alternative:** A library that clients use directly. Rejected because multiple concurrent Bluetooth connections are not reliably possible, and each client would duplicate connection management logic.
