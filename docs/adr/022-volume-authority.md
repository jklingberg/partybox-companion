# ADR-022: Volume Authority Model

**Status:** Accepted
**Date:** 2026-06-29

## Context

The appliance tracks speaker volume from multiple sources:

- **Spotify Connect** — librespot reports volume changes via stderr
- **AirPlay** — shairport-sync will report volume changes similarly (planned)
- **REST API** — clients write volume directly via `POST /api/v1/volume`
- **BLE hardware** — the speaker can report its actual output level via a GATT
  notification (opcode not yet confirmed from hardware captures)

These sources disagree at times. Spotify requests 80%; the user turns the
physical knob to 50%. Which value does `GET /api/v1/volume` return? When should
Companion update its in-memory state?

This ADR defines the ownership model across two implementation phases so that
the answer is consistent and the correct architecture is in place before BLE
volume is implemented.

## Decision

### Phase 1 — BLE volume unavailable (current)

`VolumeState` is the effective source of truth. It lives in `companion` and
holds the most recently known volume together with the source that reported it.

Sources update it directly:

| Source | Call |
|--------|------|
| Spotify Connect | `VolumeState.update(percent, "spotify")` |
| REST API write | `VolumeState.update(percent, "api")` |
| AirPlay (future) | `VolumeState.update(percent, "airplay")` |

`GET /api/v1/volume` tries BLE first; because `VolumeCapability.get()` raises
`NotImplementedError`, it falls through to `VolumeState`. Optimistic updates
(writing `VolumeState` on REST write) are correct in this phase because there
is no hardware source to contradict them.

### Phase 2 — BLE volume implemented

**The PartyBox hardware becomes the authoritative source.**

The speaker reports its actual output level via BLE GATT notifications. The
intended flow is:

```
Physical knob turn / BLE SET confirmation
        ↓
partybox SDK (VolumeCapability notification handler)
        ↓
partyboxd DeviceManager emits VolumeChangedEvent
        ↓
companion _forward_ble_volume task receives event
        ↓
VolumeState.update(percent, "ble")
```

In Phase 2:

1. `GET /api/v1/volume` — BLE `get()` returns the confirmed hardware level
   directly. `VolumeState` is the fallback when hardware is disconnected.

2. `POST /api/v1/volume` — the REST handler sends a BLE `set()` command. It
   does **not** update `VolumeState` directly. The hardware confirmation
   notification arrives via `VolumeChangedEvent` and updates `VolumeState`
   with `source="ble"`. Clients may observe a brief window where a GET still
   returns the previous level.

3. **Optimistic updates stop once BLE is connected.** The REST handler
   should gate the `VolumeState` write on BLE being unavailable:

   ```python
   ble_available = False
   try:
       await manager.set_volume(body.level)
       ble_available = True
   except (DeviceNotConnectedError, NotImplementedError):
       pass
   if not ble_available and volume_state is not None:
       volume_state.update(body.level, "api")
   ```

4. **Audio service updates continue.** Spotify and AirPlay update `VolumeState`
   with their respective sources regardless of BLE availability. These reflect
   the mixer level reported by the audio service, which may differ from
   hardware output level. The `source` field lets callers interpret which
   component produced the value.

### Conflict resolution

Last write wins. `VolumeState` records the most recent update from any source.
Simultaneous active audio sources are not supported (the appliance plays one
source at a time), so there is no multi-source arbitration problem.

### Layer constraint

`partyboxd` must not import from `companion`. BLE volume notifications therefore
cannot reach `VolumeState` (in `companion`) by a direct function call from
`partyboxd`. The `VolumeChangedEvent` on partyboxd's `EventBus` is the clean
crossing point: `partyboxd` emits the event; `companion` subscribes and updates
its own state.

`VolumeChangedEvent` is defined in `partyboxd/device/events.py` now (before BLE
volume is implemented) to establish the notification path and document the
intended architecture. `DeviceManager` does not emit it yet.

## `source` field

`VolumeState` and `GET /api/v1/volume` expose a `source` field:

| Value | Meaning |
|-------|---------|
| `"ble"` | Confirmed by hardware notification |
| `"spotify"` | Reported by librespot |
| `"airplay"` | Reported by shairport-sync |
| `"api"` | Set directly via REST `POST /api/v1/volume` |
| `null` | Not yet known |

This field lets external clients (Home Assistant, diagnostics, Portal) detect
which component currently owns the volume state, and which implementation phase
the appliance is in.

## Consequences

- When BLE volume is implemented, the `POST /api/v1/volume` handler must be
  updated to gate the optimistic `VolumeState` write as described above.
- No external API changes are needed at the phase boundary; the `source` field
  transitions from `"api"/"spotify"` to `"ble"` automatically.
- If the PartyBox hardware does not send volume confirmation notifications (not
  yet verified), Phase 2 falls back to optimistic updates. Revisit this ADR
  once hardware behaviour is confirmed from BLE captures.

## Alternatives considered

**Move `VolumeState` to `partyboxd`.**
Rejected: `partyboxd` would then need to know about Spotify and AirPlay to
update it — a downward dependency violation. The aggregation point must live in
`companion`, which already knows all audio sources.

**Optimistic updates always.**
Rejected for Phase 2: hardware truth would be ignored, and clients observing
both the REST API and the physical speaker would see disagreements.

**Confirmed updates always (including Phase 1).**
Rejected: there is nothing to confirm against while BLE is unavailable.
Optimistic is the only option in Phase 1.
