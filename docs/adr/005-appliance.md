# ADR-005: Appliance Layer (companion)

**Status:** Accepted

---

## Context

The daemon (`partyboxd`) handles Bluetooth and exposes an HTTP API. That is useful but incomplete for most users. A typical user also wants:

- Spotify Connect (via librespot)
- AirPlay (via shairport-sync)
- A browser-based administrative interface (Companion Portal)
- A CLI for scripting

These concerns could be bundled into the daemon, or separated into a third layer.

## Decision

A third package (`companion`, distributed as `partybox-companion`) provides the full appliance experience on top of the daemon.

`companion` extends `partyboxd`'s FastAPI application **in-process**: the daemon provides a `create_app()` factory; the companion calls it, then mounts its own routes and serves the Companion Portal. Single process, single port.

```python
# partyboxd exposes
def create_app(settings: DaemonSettings) -> FastAPI: ...

# companion assembles the full appliance
from partyboxd.api import create_app as create_daemon_app

def create_companion_app(settings: CompanionSettings) -> FastAPI:
    app = create_daemon_app(settings.daemon)
    app.mount("/", webui_router)
    app.include_router(services_router, prefix="/api/v1/services")
    return app
```

Users can install `partyboxd` alone for a minimal footprint (headless API, no Spotify, no Companion Portal), or `partybox-companion` for the complete appliance.

## Consequences

**Benefits:**
- Clean separation: the daemon is independently useful and testable.
- The appliance adds capabilities without modifying the daemon.
- `partybox-companion` is what most users install; `partyboxd` is for power users who want a minimal footprint.
- Single port, single process — simpler than running daemon + appliance as separate services.

**Accepted trade-offs:**
- In-process extension means the companion and daemon share a process. A crash in a companion service can affect the daemon.
- Contributors who only want to work on the daemon must still have the companion package structure in mind to understand how `create_app()` is consumed.

**Rejected alternative:** The companion as a separate HTTP service that proxies to the daemon. Rejected because it doubles the number of processes and introduces a proxy layer with no benefit at this scale.
