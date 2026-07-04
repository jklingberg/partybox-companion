# ADR-007: HTTP over TCP Only (No Unix Domain Sockets)

**Status:** Accepted

> **Historical note:** the CLI discussed in this ADR was ultimately removed before the first public release. The decision to standardise on TCP still holds on its own merits (the Portal and all HTTP clients use it); the architectural reasoning is preserved as part of the project's history.

---

## Context

The CLI and Companion Portal both need to communicate with the daemon. Unix Domain Sockets (UDS) are a common choice for local IPC: they are faster than TCP loopback and have tighter access control via filesystem permissions.

The question was whether to use UDS for the CLI (local process-to-process communication) alongside TCP for the Companion Portal, or to use TCP for everything.

## Decision

HTTP over TCP only. No UDS.

The CLI talks to the daemon at `http://localhost:8080` (or a configured URL). The Companion Portal talks to the same endpoint from the browser. Both use the same transport.

## Consequences

**Benefits:**
- Single transport, single port. One server, one auth mechanism, one set of routes.
- Browsers cannot use UDS. If the Companion Portal were to use TCP and the CLI were to use UDS, there would be two separate server configurations, two auth mechanisms, and two code paths to maintain.
- Remote access (e.g. `PARTYBOX_URL=http://192.168.1.x:8080`) works for both the CLI and the Companion Portal without any additional configuration.
- HTTP clients are universal. Any language, any tool can talk to the daemon.

**Accepted trade-offs:**
- Slightly higher latency than UDS for CLI commands. In practice, for a speaker control API, this is imperceptible.
- TCP requires an API key for security on untrusted networks. UDS would inherit OS-level access control. The API key approach is simpler to reason about across all clients.

**Rejected alternative:** UDS for the CLI + TCP for the Companion Portal. Rejected because dual transport doubles the maintenance surface for zero user-facing benefit. The Companion Portal requiring TCP means TCP is already a requirement; there is no point adding UDS on top.
