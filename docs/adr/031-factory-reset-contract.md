# ADR-031 — Factory Reset Contract

**Status:** Accepted
**Date:** 2026-07-05
**Milestone:** Factory reset (PR #47)

---

## Context

The appliance persists state that survives a power cycle: the Portal configuration
(`/var/lib/companion/config.json` — device name, Spotify Connect name, bitrate,
remembered speaker address) and the Bluetooth bond (`/var/lib/bluetooth/…` plus the
BlueZ `Device1` object). Pulling the power restarts the appliance but clears none of
it. Before this feature, the only way to return the appliance to a clean state — to
hand it to someone else, or to recover from a stale/broken Bluetooth bond — was to
re-flash the SD card.

An appliance should always have a software path back to its as-shipped state.
[ADR-001](001-project-vision.md) frames this product as an appliance, not a library;
"you must re-flash to recover" is a library-grade escape hatch, not an appliance one.

PR #47 adds a factory reset that clears the four pieces of state the appliance owns
today. What that PR does *not* yet pin down — and what this ADR exists to fix — is the
**contract**: what "factory reset" promises, so that future contributors know what
belongs in it as the appliance grows. Two candidate definitions were on the table:

1. *Return the appliance to the state immediately after first boot.*
2. *Remove every piece of user-owned state.*

They coincide today. They can diverge tomorrow (e.g. shipped defaults that are not
"empty", or state that is machine-specific but not user-authored).

---

## Decision

**The contract is definition 1: after a factory reset, the appliance is
indistinguishable from a freshly-flashed image on first boot.**

This is the primary, testable promise. Definition 2 ("remove all user-owned state") is
the operational corollary — it is *how* we usually achieve indistinguishability — but
where the two disagree, indistinguishability from a fresh image wins. Concretely:

- Any state written at runtime that a fresh image does **not** have must be cleared or
  reverted by a factory reset.
- State that a fresh image **does** ship with (defaults, baked-in config) must be
  restored to that shipped value, not merely deleted, if deletion would change
  behaviour.
- Machine-identity that a fresh image regenerates on its own at first boot (host keys,
  self-signed certificates, etc.) does **not** need to be reset by us — first boot will
  re-establish it — but it must not be *left in a state a fresh image would never
  produce*.

**In scope as of PR #47** (the four current participants):

| State | Reset action |
|---|---|
| Portal configuration (`config.json`) | Delete the file (`ConfigStore.reset()`) — a fresh image has no file; see below |
| Remembered speaker address | Cleared as part of `config.json`; live copy dropped via `AudioService.forget()` |
| Bluetooth bond | Removed via `Adapter1.RemoveDevice` (`PairingService.forget()` → `BluezClient.remove_device()`) |
| Spotify Connect name | Reverts with `config.json`; librespot relaunched with defaults |

**Deleting `config.json` rather than writing a default `PortalConfig()`** is deliberate
and load-bearing for the contract: a factory-fresh appliance simply has no
configuration file (`ConfigStore.read()` returns `PortalConfig()` when the file is
absent). Writing a serialized default object would produce an on-disk state that a
fresh image never has, weakening "indistinguishable from fresh". See
[ADR-017](017-runtime-layout.md) for the runtime-layout invariants this preserves.

**Rule for future contributors:** when you add appliance state that is written at
runtime and is not present on a fresh image — AirPlay pairing/config, future Portal
preferences, diagnostics history, update preferences, caches, provisioning state — you
must add its teardown to the factory-reset flow, or explicitly document why it is
exempt (e.g. it is machine-identity a fresh boot regenerates). The acceptance test is
always the same: *after reset, is this appliance distinguishable from one just flashed?*

### Recognized but not yet implemented — Wi-Fi credentials

The saved Wi-Fi connection is the clearest existing example of the rule above and is a
planned near-term addition, **not implemented in PR #47**. NetworkManager persists the
provisioned network (SSID + PSK) as a connection profile under
`/etc/NetworkManager/system-connections/`; a fresh image has none and boots straight
into AP provisioning mode ([ADR-021](021-network-provisioning.md)). By the contract, a
factory reset should delete that profile (`nmcli connection delete`) so the appliance
returns to provisioning mode. `ProvisioningService` already shells out to `nmcli` with
the required NetworkManager permissions, so no new privilege is involved.

The reason it is deferred to its own change rather than folded into PR #47 is a
**connectivity caveat unique to this participant**: the factory reset is normally
triggered over the very Wi-Fi link being deleted. Removing the active profile drops the
appliance off the network and back into AP mode *mid-request*, so the HTTP `204` will
usually never reach the browser. Wi-Fi reset must therefore run **last**, and the Portal
must set expectations before the call ("the appliance will disconnect and reappear as a
Wi-Fi hotspot — reconnect to finish"). That UX contract, not the deletion, is the work.

---

## Alternatives considered

### Definition 2 as the primary contract ("remove all user-owned state")

A data-ownership framing: reset erases everything the user touched. Rejected as the
*primary* definition because "user-owned" is ambiguous at the edges — is a
machine-generated certificate user-owned? is a shipped default the user never changed? —
whereas "indistinguishable from a fresh flash" is unambiguous and directly testable
against a real reflashed image. Definition 2 survives as the everyday heuristic; it is
subordinate to definition 1 when they conflict.

### Leave the contract implicit / decide per-addition

Rejected: this is exactly the "load-bearing decision with no recorded rationale" that
[ADR-030](030-bluez-gatt-configuration.md) warns about. Without a written contract, each
future contributor re-derives (or forgets) whether their new state belongs in the reset,
and the promise erodes silently.

### Introduce a `FactoryResetService` now

The reset is currently orchestrated directly in the services router
(`POST /api/v1/factory-reset`), which coordinates four participants. Extracting a
dedicated `FactoryResetService` was considered and **deferred**: with four participants
and a linear, race-free sequence, a service abstraction would be indirection without
payoff. **Revisit trigger:** when the participant list grows past roughly the current
handful — specifically once reset must coordinate additional appliance *services* with
their own lifecycles (AirPlay, provisioning, certificate stores), rather than plain
state files — move the orchestration into a `FactoryResetService` so the router stays a
thin transport layer.

---

## Consequences

- The product promise is explicit and testable: **factory reset == fresh install.**
  The hardware acceptance check is to reset, re-pair, and confirm the appliance behaves
  exactly like a freshly-flashed image (pairing, audio, Spotify, Portal state).
- Future state additions have a clear home and a clear test; the reset does not silently
  fall behind the appliance's growth.

### Known limitation — partial failures are not surfaced (future work)

Bond removal is **best-effort**: if `Adapter1.RemoveDevice` fails, the reset logs a
warning and continues, because clearing the configuration is still valuable and the bond
can be re-established by re-pairing. The endpoint therefore always returns `204`, even
when the on-disk config was cleared but the bond removal failed. In that (rare) case the
appliance is *not* fully indistinguishable from a fresh image, yet the user is told the
reset succeeded.

This is an accepted limitation for the current scope, not a permanent contract. A future
enhancement should propagate per-step outcomes so the Portal can report **"Factory reset
completed with warnings"** rather than always implying a perfect reset — without making
a single failed step abort the whole reset. Deferred deliberately to keep PR #47's API
surface minimal; captured here so the gap is visible and owned.
