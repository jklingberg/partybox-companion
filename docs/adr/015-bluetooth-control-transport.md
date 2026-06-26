# ADR-015: Bluetooth Control Transport is BLE GATT (via bleak)

**Status:** Accepted

**Amends:** [ADR-003](003-sdk-first.md) (relaxes its zero-runtime-dependency constraint)

---

## Context

[ADR-003](003-sdk-first.md) established `partybox` as a zero-runtime-dependency SDK, on the stated assumption that speaker control runs over **Bluetooth Classic SPP/RFCOMM** — which Python's standard library can drive directly via `socket`. The early `docs/reverse-engineering/protocol.md` recorded the same assumption.

Hardware verification on 2026-06-26 disproved it. An SDP browse of a real PartyBox 520 shows **no SPP/RFCOMM service**. Bluetooth Classic carries only audio (A2DP) and media transport controls (AVRCP). Speaker **control** — power, and everything the SDK exists to do — runs over **BLE GATT**:

- Control service `65786365-6c70-6f69-6e74-2e636f6d0000` (UUID base is ASCII `"excelpoint.com"`, the vendor).
- TX characteristic `…0002` (`write`) — commands host → speaker.
- RX characteristic `…0001` (`notify`) — responses speaker → host.
- The LE control identity address is distinct from the Classic audio address.

This was confirmed both from the original macOS `bleak` capture and reproduced on Linux/BlueZ. See [discoveries.md](../reverse-engineering/discoveries.md).

The standard library has no GATT client. Driving GATT on Linux means BlueZ's D-Bus API; doing it portably and robustly (pairing, bonding, MTU negotiation, notifications) is a large, brittle undertaking to hand-roll. `bleak` is the de-facto Python BLE library, is cross-platform (BlueZ / CoreBluetooth / WinRT), ships type information, and was already the tool used to reverse-engineer the protocol.

## Decision

The `partybox` SDK uses **`bleak`** as its Bluetooth transport. The zero-runtime-dependency constraint of ADR-003 is **relaxed to a minimal-dependency policy**: `bleak` (and its own transitive deps) is permitted because the SDK's core purpose — talking to the speaker — is not achievable in pure stdlib given the real transport.

All other ADR-003 constraints stand unchanged: no networking beyond Bluetooth, no subprocess management, no daemon lifecycle, no configuration loading, no knowledge of the layers above.

The transport abstraction is reframed around GATT rather than a byte stream:

- `ControlTransport.write(data)` writes a command frame to the TX characteristic.
- `ControlTransport.receive()` returns the next notification payload from the RX characteristic.

(The earlier `read(size)` stream API suited RFCOMM; GATT is message-oriented, so notifications are surfaced whole.)

`BleakTransport` is the production implementation; `MockTransport` keeps the same interface for hardware-free testing. Device discovery (`scanner.py`) scans **LE**, not Classic inquiry.

## Consequences

**Benefits:**
- The SDK can actually control the speaker — the prior design could not.
- `bleak` handles LE connection, pairing/bonding, MTU, and notifications across platforms, including macOS for development.
- A message-oriented transport maps more cleanly onto GATT than a byte stream did.

**Accepted trade-offs:**
- `partybox` is no longer dependency-free. The "lightweight and auditable" benefit of ADR-003 is weakened, though `bleak` is widely used and maintained.
- CI must keep `bleak` importable (it imports without a Bluetooth adapter, so this is fine); transport behaviour against real hardware remains `@pytest.mark.hardware`.
- The pure-stdlib L2CAP/ATT alternative was rejected: it preserves zero-dependency but requires hand-rolling a GATT client and LE bonding over raw sockets — high effort and risk for no user-facing benefit.
