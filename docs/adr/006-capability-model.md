# ADR-006: Capability-Based Model Support

**Status:** Accepted

---

## Context

The JBL PartyBox product line has many models with different feature sets. The PartyBox 520 has LED lighting; the PartyBox 110 does not. Portable models have batteries; stationary models do not. Some models have EQ control; others do not.

There are two common approaches to this problem:

(a) **Model-specific code** — a lookup table mapping model identifiers to supported features, with per-model branches in the device implementation.

(b) **Capability-based detection** — the device reports what it supports at runtime; the SDK exposes features as typed optional properties that are `None` when unsupported.

## Decision

The SDK uses a capability-based model. Optional features are exposed as typed optional properties on `Device`:

```python
class Device:
    # Required — guaranteed present on every device
    @property
    def power(self) -> PowerCapability: ...
    @property
    def device_info(self) -> DeviceInfoCapability: ...

    # Optional — None if unsupported by this device
    @property
    def battery(self) -> BatteryCapability | None: ...
    @property
    def lights(self) -> LightsCapability | None: ...
    @property
    def eq(self) -> EQCapability | None: ...
    @property
    def microphone(self) -> MicrophoneCapability | None: ...
```

At connection time, `PartyBoxDevice` probes the device via the protocol and populates the capability registry. Callers check for `None` to determine support.

> **See also [ADR-010](010-sdk-scope.md)**, which defines the scope of which capabilities belong in the SDK. The pattern described here applies to all future capabilities regardless of scope.

## Consequences

**Benefits:**
- Adding support for a new model typically requires no code changes — the capability set is determined at runtime.
- Adding a new capability type (new file in `device/capabilities/`) does not affect any existing code.
- No model database to maintain. No risk of the database going stale with firmware updates.
- The type system expresses the design intent: callers are forced to handle the `None` case.

**Accepted trade-offs:**
- If the protocol does not provide a reliable capability query mechanism, we must fall back to probing or to a static model table. This is deferred until the protocol is better understood.
- Capability detection via protocol probing adds latency at connection time.

**Rejected alternative:** A static model lookup table keyed on Bluetooth device name or hardware identifier. Rejected because it requires manual updates for every new model and firmware version, and it cannot handle edge cases like third-party or modified firmware.
