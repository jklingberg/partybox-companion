# ADR-033: Speaker Power State — Standby Detection via Control-Plane Liveness Probing

**Status:** Accepted

---

## Context

The Companion Portal's `speaker_state` field (`"off" | "standby" | "on"`, exposed on `GET /api/v1/health` and pushed via `SpeakerStateChangedEvent`) drives the Portal's power-toggle UI (`docs/design/portal-redesign.md`, ship-order step 2). Showing "Turn off" on a speaker that is actually asleep is a visible, confusing bug — this happened in production and is the motivating incident for this ADR.

`"off"` is unambiguous: the BLE control connection itself is down (`DeviceManager.snapshot.connected`, backed by [ADR-015](015-bluetooth-control-transport.md)'s GATT transport).

`"standby"` is not so simple. The PartyBox auto-idles into a low-power state (amp off, most LEDs off) without dropping the BLE GATT link — the control MCU keeps the connection alive (see the M3 audio-viability spike and `docs/reverse-engineering/open-questions.md`, which notes bonding and control both require the speaker to be reachable over BLE regardless of amp state). No confirmed vendor-protocol notification distinguishes this auto-idle transition: the only known power-state push, opcode `0x12` tag `0x36` (`docs/reverse-engineering/discoveries.md`), has only been observed as a direct response to an explicit `AA 03 01 05`/`AA 03 01 04` power on/off command — never as a spontaneous push when the speaker times out into standby on its own.

The first implementation inferred standby from `has_battery=True AND battery reading absent`. `has_battery` is set only once battery *capability* is confirmed by a successful probe ([ADR-032](032-capability-probing.md)). This created an unsatisfiable precondition: a speaker that was already asleep at the moment the BLE connection was established would fail that very first battery probe, so `has_battery` would never become `True` for the rest of the session — and the standby condition could then never be met. The Portal showed "Turn off" on a speaker asleep for its entire session (bug fixed 2026-07-06/07, commit `1dc4d61`).

## Decision

Detect standby by periodically re-probing the vendor control protocol for *any* response, independent of whether battery capability has ever been confirmed:

- At the daemon's existing 15 s connection health-check cadence (`_HEALTH_CHECK_INTERVAL`, `DeviceManager._drain_with_health_check`), attempt a battery-status query first (a more direct, power-state-specific signal when the model has one), falling back to a firmware-version query — universal, since every known PartyBox model answers opcode `0x21` when awake ([ADR-032](032-capability-probing.md); `docs/reverse-engineering/discoveries.md`).
- Track "did the speaker answer anything just now" (`speaker_awake`) as a signal orthogonal to "has battery capability ever been confirmed" (`has_battery`). `speaker_state` derives from connection state plus `speaker_awake` only — never from `has_battery`.
- Tolerate `_LIVENESS_MISS_LIMIT = 2` consecutive misses (across both probes combined) before flipping to standby, so a single dropped or slow response doesn't flap the state; one successful probe immediately clears back to "on".
- Run the same dual probe immediately in `_refresh()` at connect time, so a speaker already asleep when the BLE link is established is detected as standby from the first snapshot — not gated behind a capability-confirmation step that, for that speaker, will never happen.

**Validated on hardware (2026-07-08):** with the speaker physically in standby (confirmed visually — all LEDs off except the power LED), both probes reliably timed out on every health-check cycle, and the daemon correctly reported `speaker_state: "standby"`. Detection converged within one health-check interval on a connection that transitioned to standby while live, and immediately at connect time for a connection established while already asleep. This confirms the premise the decision rests on: the vendor MCU actually stops answering control-plane queries during auto-idle standby — it is not merely the amp/audio path powering down while the MCU stays fully responsive, which would have made this whole approach unworkable.

## Consequences

**Benefits:**
- Standby detection no longer depends on a capability-confirmation side effect; it works uniformly whether or not the speaker was ever observed "awake" during the session.
- Reuses the existing health-check timer — no new polling loop, no new opcode, no protocol work required.
- The battery → firmware two-tier probe means models without a battery still get standby detection via the universal firmware query alone.

**Accepted trade-offs:**
- Detection latency of up to roughly `_LIVENESS_MISS_LIMIT × _HEALTH_CHECK_INTERVAL` plus per-probe timeout (currently up to ~30–45 s) when a live connection transitions from on to standby. Acceptable for a coarse power-state indicator; not suitable for anything latency-sensitive.
- Every health-check cycle now performs a real vendor-protocol round trip even when nothing has changed, adding minor BLE control traffic every 15 s for the life of the connection.
- No distinction is possible between "this firmware doesn't implement these opcodes" and "the speaker is asleep" — both look identical (timeout) to the recurring probe. `_detect_battery()` at connect time already treats a battery timeout as "no battery capability", not "asleep" ([ADR-032](032-capability-probing.md)); the recurring liveness probe deliberately does not attempt that distinction on every cycle, since capability was already resolved once.

## Rejected alternatives

- **Wait for a dedicated standby/idle notification.** None is confirmed on the wire. The only known power-state push (opcode `0x12` tag `0x36`) is tied to explicit power commands (`docs/reverse-engineering/discoveries.md`), and no capture shows it firing on auto-idle. Building against an opcode behavior that has not been captured would violate this project's reverse-engineering discipline (`docs/reverse-engineering/guide.md` — validate with a capture before implementing).
- **Infer standby purely from BLE connection state.** Already rejected by the original bug's own history: the BLE control link stays up through standby, so `connected` alone can only ever distinguish "off" from "not off" — never "standby" from "on".
- **Gate standby on confirmed battery capability** (the original design). Rejected: this is precisely the unsatisfiable-precondition bug this ADR's decision fixes.

Related: [ADR-032](032-capability-probing.md) (the connect-time probe-for-capability pattern this decision reuses at runtime for liveness rather than capability), [ADR-028](028-audio-readiness-model.md) (the daemon's other periodic-probe health model, for A2DP audio readiness).

## Follow-up: `"unreachable"` state (2026-07-18)

A live incident exposed a gap in the original three-state model this ADR defined. `"off"` was framed above as "unambiguous: the BLE control connection itself is down" — true as far as it goes, but it collapses two physically different situations into one label: the speaker is genuinely powered off (or out of range), and the speaker is powered on but *our* control link can't currently reach it (a wedged local controller, radio interference — see [ADR-028](028-audio-readiness-model.md)'s HCI UART corruption findings, [ADR-039](039-ble-controller-wedge-self-heal.md)). Observed live: the appliance's Bluetooth controller suffered HCI UART corruption badly enough that the control link stayed down for 15+ minutes while the speaker was confirmed powered throughout (audible, playing from another Bluetooth source) — the Portal reported "Speaker is powered off" the entire time.

**Revised decision:** `StatusSnapshot` gains `beacon_seen: bool` — whether the most recent BLE scan saw the speaker's FDDF service-data beacon (`partybox.bluetooth.scanner.HARMAN_FDDF_UUID`), which the speaker broadcasts continuously while powered, independently of whether its named connectable control advert is currently present or reachable. `speaker_state` becomes `"off" | "unreachable" | "standby" | "on"`: disconnected *and* `beacon_seen` is `"unreachable"` rather than `"off"`. Detected at effectively zero extra cost — `partybox.bluetooth.scanner.Scanner.discover_with_presence()` reads it from the same `BleakScanner.discover()` call `DeviceManager`'s reconnect loop already makes; no second scan.

`"unreachable"` is treated as generously as `"standby"` by [ADR-038](038-idle-battery-shutdown.md)'s idle-battery-shutdown timer (not `"off"`'s short debounce): the beacon is exactly the confirmation of continued power draw that would otherwise be missing, so there is no reason to judge it more urgently than a speaker known to be merely asleep.

The Portal's `deriveScene()` gained a corresponding scene (distinct from the pre-existing `Scene.UNREACHABLE`, which means Companion itself is unreachable) explaining the real situation — the speaker is fine, the fix is on the appliance/Bluetooth side — instead of telling the user to check whether the speaker is plugged in.
