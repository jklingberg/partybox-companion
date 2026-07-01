# ADR-027 — Bluetooth Bonding Architecture: D-Bus Discovery, Scoped Bondable Mode, Immediate Pairing

**Status:** Accepted
**Date:** 2026-06-30
**Milestone:** M16 (follow-up)
**Amends:** [ADR-026](026-bluetooth-audio-pairing.md) — supersedes its "auto-detect BR/EDR address from LE service data" rejection and its `bluetoothctl`-based pairing mechanics. The two-state connection model, REST surface, and Portal UX from ADR-026 are unchanged.

---

## Context

A structured investigation (btmon HCI traces, `btmgmt`/`bluetoothctl` against the kernel and BlueZ directly) was run to determine why `PairingService` ([ADR-026](026-bluetooth-audio-pairing.md)) never produced a persistent BR/EDR bond with the JBL PartyBox 520 — `bluetoothctl info` consistently reported `Paired: yes, Bonded: no`, and the speaker required re-pairing after every disconnect. Three independent, compounding root causes were confirmed at the protocol level:

1. **Adapter never entered bondable mode.** `PairingService._pair()` calls `bluetoothctl pair <mac>` without first requesting bondable/pairable mode, and `AlwaysPairable` is unset in `main.conf`. btmon showed `IO Capability Request Reply: Authentication = No Bonding (0x00)` during every prior pairing attempt. SSP can complete with `0x00`, but no persistent link key is requested or issued — exactly the `Bonded: no` symptom. With bondable explicitly enabled, the same exchange showed `Authentication = Dedicated Bonding (0x02)`, and the JBL issued a persistent Secure Connections (P-256) link key (`type 0x07, store_hint=1`) that survived `bluetooth.service` and `companion.service` (including its `ExecStartPre` HCI reset, [ADR-023](023-hci-controller-reset-on-startup.md)) restarts.

2. **The JBL has a short BR/EDR pairing window**, separate from its much longer LE advertising window. After the window lapses, BR/EDR SSP attempts fail with `Simple Pairing Complete: Pairing Not Allowed (0x18)` — confirmed by two failed attempts against a speaker that had been in "pairing mode" for 30+ minutes, versus immediate success against a freshly-pressed speaker with identical adapter settings. The exact duration was not measured further; see Decision 4.

3. **The JBL does not respond to BR/EDR inquiry at all** (a 10-second explicit `HCI Inquiry` against a speaker in pairing mode returned zero results), and `bluetoothctl scan on` issues an LE-only scan in the BlueZ version under test — so `_scan_for_jbl()` can structurally never discover the BR/EDR address via scanning. The address is, however, present in the JBL's LE advertisement, in Service Data (AD type `0x16`) under Harman's vendor UUID `0xfddf`, bytes 11–16. ADR-026 considered and rejected "auto-detect from LE service data," but that conclusion was based on AD type `0x1B` ("LE Bluetooth Device Address," an unrelated optional field) — the wrong AD structure. The address is reliably present; it was being looked for in the wrong place.

The full investigation trace and root-cause writeup are recorded in project memory (`m16-bonding-root-cause`).

This ADR addresses the architectural question raised after that investigation: not just *what* was wrong, but what the **production design** should be, since "drive `bluetoothctl` harder" was rejected as papering over the underlying issues with more CLI-shaped workarounds.

---

## Decision

### 1. `PairingService` drives BlueZ over D-Bus, not `bluetoothctl`/`btmgmt`

`companion` already depends transitively on `dbus-fast` (`bleak`, used by `partybox` for BLE GATT — [ADR-015](015-bluetooth-control-transport.md) — uses `dbus-fast` as its Linux backend). `PairingService` will use it directly against `org.bluez`:

- `Adapter1.SetDiscoveryFilter({"Transport": "le"})` / `StartDiscovery()` / `StopDiscovery()` for discovery.
- `Device1.ServiceData` (a structured `{UUID: bytes}` property BlueZ populates as it parses each advertisement) instead of scraping `bluetoothctl info` text — this is what makes the FDDF extraction in Decision 3 possible at all; there is no CLI surface for raw AD structures.
- `Device1.Pair()` / `.Trusted = True` / `.Connect()`, called against a known BR/EDR address — typed D-Bus exceptions (`org.bluez.Error.AuthenticationFailed`, `...AlreadyExists`, etc.) replace string-matching `bluetoothctl` stdout for `"pairing successful"` / `"alreadyexists"`.
- A registered `org.bluez.Agent1` with `NoInputNoOutput` capability, scoped to the pairing operation, replaces relying on `bluetoothctl`'s built-in default agent.

`companion`'s `pyproject.toml` will declare `dbus-fast` directly rather than relying on it arriving transitively through `partybox` — it's now load-bearing for `companion`, not just an implementation detail of the SDK's BLE transport.

**`btmgmt` is excluded from production code entirely.** It was valuable for this investigation specifically *because* it bypasses `bluetoothd` and talks to the kernel MGMT socket directly — that isolation is what let the investigation separate "is this a BlueZ policy problem or a kernel/controller problem." In production, bypassing `bluetoothd` risks its D-Bus object model (`Device1.Bonded`, `GetManagedObjects()`) silently diverging from actual kernel state, since `bluetoothd` has no visibility into changes `btmgmt` makes underneath it.

`AudioService`'s existing `bluetoothctl`-based connection polling is **not** changed by this ADR — it has the same CLI-fragility shape, but migrating it is a separate, larger change (it would also enable push-based `PropertiesChanged` connection events instead of polling) and is out of scope here.

### 2. Bondable mode is scoped to the pairing operation only

The adapter is set bondable (`Adapter1.Pairable = True`, or equivalently `Discoverable`/pairable timeout) only for the duration of an explicit, user-initiated pairing attempt — set at the start of `_do_pair()`, cleared in a `finally` regardless of success, failure, or cancellation. It is never left bondable as a standing condition.

This appliance's agent capability is `NoInputNoOutput`, which forces the **Just Works** SSP association model — there is no PIN exchange and no user-visible confirmation step on the Pi's side. If the adapter were permanently bondable, any nearby BR/EDR device requesting pairing would bond silently, with zero consent gesture available to the user. A "PartyBox" is, by design, used in rooms full of other people's phones and Bluetooth peripherals; permanent bondability under Just Works is a real exposure for this product, not a theoretical one. Scoping bondable mode to the period when a human has just tapped "Start Pairing" in the Portal means the Pi only accepts new bonds while someone is actively standing at the speaker — symmetric with the JBL's own short, button-press-gated pairing window (Decision 4 / Context finding 2).

### 3. LE service-data extraction (Harman FDDF) is the canonical discovery mechanism

`PairingService` discovers the BR/EDR pairing target by parsing `Device1.ServiceData["0000fddf-0000-1000-8000-00805f9b34fb"]` from LE advertisements during discovery and extracting bytes 11–16 as the BR/EDR public address, rather than filtering scan results by address type (`_is_public_address()`) or falling back to a previously-cached device (`_find_cached_jbl()`).

**FDDF is a Harman International proprietary vendor UUID, not a Bluetooth SIG or GATT standard.** UUID `0xfddf` is unregistered with the Bluetooth SIG and exists solely as an internal Harman device-discovery mechanism. Only JBL/Harman speakers advertise this AD structure; no other manufacturer's devices will carry it. The payload layout — specifically, the BR/EDR public address at bytes 11–16 of the Service Data AD structure — was derived from a real btmon capture of a PartyBox 520 (see `docs/reverse-engineering/`) and is not documented in any public Harman specification. This is a reverse-engineered, JBL/Harman-specific discovery mechanism, not a transferable Bluetooth feature. Any extension of this appliance to support non-Harman speakers will require a different discovery strategy for that speaker's canonical BR/EDR address.

This is canonical, not a fallback, because:

- BR/EDR inquiry is a structural dead end for this device (Context finding 3) — there is no scenario where scan-and-filter-by-address-type succeeds against a JBL that hasn't already been paired before.
- It is deterministic: the vendor is directly publishing the BR/EDR address for exactly this purpose, rather than inferring intent from a fuzzy name match (`"jbl" in name.lower()`) against whatever the LE scan happens to return.
- It removes the hidden dependency the current implementation has on `_find_cached_jbl()` succeeding — which only works because of a *prior* successful pairing. A clean-state first boot (the actual first-time-setup case this service exists for) has no cache to fall back to and would otherwise always fail.

A cheap `"JBL" in local_name` check is retained alongside the FDDF parse as a sanity guard against an unrelated device coincidentally advertising under the same vendor UUID — a guard, not the primary selection mechanism.

This supersedes the "auto-detect BR/EDR address from LE service data" rejection in ADR-026's alternatives section, which was based on checking AD type `0x1B` (LE Bluetooth Device Address — an unrelated, optional field) rather than AD type `0x16` Service Data under Harman's `0xfddf` UUID, where the address is in fact reliably present.

### 4. Discovery and pairing are a single event-driven transition, not sequential phases

`_do_pair()` no longer runs a fixed-duration scan to completion and then attempts pairing with whatever it found. It starts discovery and reacts to the **first** `PropertiesChanged`/`InterfacesAdded` signal that includes a valid Harman FDDF service-data payload: discovery is stopped and `Pair()` is called immediately against the extracted address. No polling interval (the current implementation polls `bluetoothctl devices` every 2s) and no waiting out the remainder of a scan window sit between discovery and pairing.

This follows directly from Context finding 2 (short BR/EDR pairing window): the previous architecture could spend most or all of a 60-second scan window before even attempting to pair, which risked attempting pairing after the JBL's own window had already closed. The fix is structural — collapse "found it" and "pair now" into the same step — rather than tuning timeout values against an unmeasured and possibly inconsistent window duration. The 60-second budget becomes an outer give-up timeout for the whole operation (no candidate found at all), not a scan duration that is always fully consumed first.

### 5. A persistent link key is required for appliance-quality reconnect

Stated explicitly because it's the reason Decisions 1–4 matter: this appliance has no UI for re-running a pairing ceremony on every boot or reconnect, and the Portal experience (ADR-026) is built around pairing being a rare, one-time setup action. Without a persisted BR/EDR link key (`Bonded: yes`, `[LinkKey]` present in BlueZ's device store), every disconnect — including the HCI reset that runs on every `companion.service` start ([ADR-023](023-hci-controller-reset-on-startup.md)) — would require the user to physically return to the speaker and press its Bluetooth button again. A persistent key lets BlueZ auto-reconnect (confirmed: ~2 seconds after `companion.service` start, with no explicit `Connect()` call needed) and is therefore treated as a hard correctness requirement of pairing, not a nice-to-have. This is the standard this ADR's other decisions are in service of.

---

## Alternatives considered

### Keep driving `bluetoothctl`, just add `pairable on` before pairing

Would fix root cause 1 (Decision 1) in isolation, but does nothing for the discovery dead-end (Decision 3) or the sequential-phases timing problem (Decision 4), and leaves `PairingService` dependent on scraping CLI text output indefinitely. Rejected as treating one symptom rather than the architecture.

### Set `AlwaysPairable = true` in `main.conf` instead of scoping bondable mode per-operation

Simpler — no code path needs to toggle adapter state. Rejected on the Just-Works security grounds in Decision 2: this would make the adapter permanently willing to silently bond with any nearby BR/EDR device, with no possible user confirmation step given the `NoInputNoOutput` agent.

### Use `btmgmt` in production, since it was proven to work in the investigation

Rejected — see Decision 1. It bypasses `bluetoothd`, risking divergence between kernel state and what BlueZ's D-Bus object model reports to the rest of `companion`.

### Measure the JBL's exact BR/EDR pairing-window duration and size the scan timeout accordingly

Rejected per explicit project direction: the correct fix is structural (pair immediately on discovery), not a timeout tuned against an unmeasured and possibly model-/firmware-version-dependent window.

---

## Consequences

- `companion`'s `pyproject.toml` gains an explicit `dbus-fast` dependency (already present transitively; this makes the direct usage honest).
- `PairingService` is rewritten against D-Bus: discovery filtering, FDDF service-data parsing, agent registration, and pair/trust/connect calls. `AudioService` is unchanged.
- Bondable mode is now adapter-global, scoped, and timing-sensitive — `_do_pair()` must clear it in a `finally` on every exit path (success, failure, timeout, cancellation) to avoid leaving the adapter unexpectedly bondable.
- First-time pairing on a clean-state image (no prior cache) now has a real discovery path, removing the previously silent dependency on `_find_cached_jbl()`.
- `_find_cached_jbl()`'s role changes from primary discovery path to a fast-path optimization (skip discovery if the address is already known and reachable) — still useful for re-pair flows, no longer load-bearing for first-time setup.
- ADR-026's "auto-detect from LE service data" alternative is corrected, not just superseded — future readers should treat this ADR's account of where the BR/EDR address lives in the advertisement as authoritative.

**Known limitation — first-match-wins discovery (v1.0).** `discover_bredr_address()` resolves on the first valid FDDF advertisement received. If two JBL/Harman speakers are simultaneously in pairing mode within Bluetooth range, the appliance will pair with whichever speaker's advertisement arrives first — there is no address disambiguation and no user-confirmation step (consistent with the `NoInputNoOutput` agent capability). This is intentional for v1.0: the expected setup ceremony is one speaker, one user, one button press. A future milestone that needs to select among multiple speakers would require an explicit selection mechanism (e.g. RSSI-based nearest-device selection, a Portal confirmation step showing the detected device name, or a QR code / NFC tap to pre-seed the target address).
