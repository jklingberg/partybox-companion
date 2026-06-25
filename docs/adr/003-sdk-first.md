# ADR-003: SDK-First Architecture

**Status:** Accepted

---

## Context

The Bluetooth protocol implementation is the most technically unique part of this project. It is an independent implementation of an undocumented protocol, and is useful to anyone who wants to interact with a JBL PartyBox programmatically — whether they want the full appliance or not.

The question is whether the protocol implementation should be:

(a) Internal implementation detail of the daemon, not reusable
(b) A standalone library that the daemon happens to use

## Decision

The protocol implementation is extracted as the `partybox` package — a standalone, zero-dependency Python SDK that can be installed and used independently of the daemon.

**Hard constraints on `partybox`:**
- No runtime dependencies beyond the Python standard library
- No networking beyond Bluetooth (no HTTP, no WebSockets)
- No subprocess management
- No daemon lifecycle
- No configuration loading
- No knowledge of Home Assistant, REST, Companion Portal, Spotify, or AirPlay

The daemon (`partyboxd`) depends on `partybox`; `partybox` has no knowledge that `partyboxd` exists.

## Consequences

**Benefits:**
- The SDK is publishable to PyPI independently. Developers can build custom integrations without taking on the daemon.
- Zero-dependency constraint keeps the SDK lightweight and auditable.
- The boundary forces clean separation: protocol logic can never accidentally leak into the daemon or client layers.
- The SDK can be tested in pure isolation with no external dependencies, making CI fast and reliable.

**Accepted trade-offs:**
- Three-package monorepo is slightly more complex to navigate than a single package.
- The strict boundary sometimes means adding a thin layer of abstraction (e.g. the `Device` ABC) where a tighter coupling would be simpler.

**Note on PyPI naming:** The name `partybox` may be taken on PyPI. Verify before publishing. Alternative names: `partybox-sdk`, `partybox-py`. The Python import name (`import partybox`) is unchanged regardless of the distribution name.
