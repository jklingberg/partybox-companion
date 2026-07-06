# ADR-032: Detect Optional Capabilities by Probing the Vendor Protocol

**Status:** Accepted

---

## Context

[ADR-006](006-capability-model.md) established that optional features are exposed as typed optional properties that are `None` when unsupported, and that `PartyBoxDevice` populates them at connect time. It left open *how* support is determined, noting only that we may "fall back to probing or to a static model table" once the protocol is better understood.

Battery support forced the question. The PartyBox 520 **has** an internal battery but exposes neither the standard BLE Battery Service (`0x180F`) nor the Device Information Service (`0x180A`) — battery data is available only through the vendor control protocol (opcode `0x9D` → `0x9E`; see [discoveries.md](../reverse-engineering/discoveries.md)). The original `BatteryCapability` detected support with `transport.has_service(0x180F)` and therefore reported the 520 as batteryless in every power state.

So GATT service discovery is not a reliable signal for this device: the capability exists, but no service advertises it. The vendor protocol, on the other hand, answers the question directly — a speaker with a battery responds to `AA 9D …`, one without stays silent.

## Decision

When the vendor protocol provides a more reliable capability signal than GATT service discovery, **detect the capability by actively probing the vendor command at connect time**: send the request, and treat a valid typed response as "present" and a timeout as "absent". GATT service discovery remains appropriate only where a device genuinely advertises a standard service.

Battery is the first instance. `_detect_battery(transport)` sends the battery-status request and awaits a `0x9E` response with a short timeout:

- a decoded `BatteryStatusResponse` → construct `BatteryCapability`;
- a `TimeoutError` → the speaker has no battery, property is `None`;
- a transport error propagates, so the caller treats it as a connection failure rather than "no battery".

This keeps the [ADR-006](006-capability-model.md) contract intact (callers still check for `None`) while sourcing the answer from the protocol.

## Consequences

**Benefits:**
- Correct for devices whose optional hardware has no corresponding GATT service (the 520 battery).
- No static model table to maintain (consistent with ADR-006's rejection of a model database).
- Evidence-driven: a capability is exposed only if the hardware actually answers for it.

**Accepted trade-offs and forward guidance:**
- **Connect-time latency.** Each probe is a request/response round-trip during `connect()`. One probe is negligible; if EQ, lighting, microphone, etc. also become probe-based, serial probing on every connection would add up. A future optimization (batching/parallelizing probes, or gating them behind a cheap model hint) is worth considering *before* the third or fourth probe lands — not now.
- **Keep probe helpers narrowly scoped.** `_detect_battery()` should stay battery-specific. If multiple capabilities become probe-based, introduce a dedicated `CapabilityDetector`/`FeatureDetector` that owns the probe-and-classify loop, rather than growing one helper into a catch-all.
- **Derived values are estimates, not transmitted fields.** Where the protocol reports raw fields but not the user-facing value, the SDK derives it the same way JBL's own app does. Battery charge % is computed as `remaining_capacity / full_charge_capacity`; the speaker transmits no percentage field. Documented so future contributors don't hunt for a "real percentage" opcode that does not exist.

**Rejected alternatives:**
- **Relying on GATT service discovery** (`0x180F`) — fails for the 520, the only combination validated on hardware to date.
- **A static model → capability table** — already rejected in ADR-006; requires manual updates per model and firmware and cannot handle modified firmware.
