# ADR-035: State Ownership and the Signal → Scene Pipeline

**Status:** Accepted

---

## Context

Across ADR-006, ADR-022, ADR-026, ADR-028, ADR-033, and ADR-034, this project has accumulated a growing set of independent state signals — BLE connectivity, derived `speaker_state`, A2DP/audio readiness, Spotify Connect status, Bluetooth pairing progress, WiFi provisioning state, WebSocket connectivity, Portal-reachability, and a Portal-local `powerPending` flag — each introduced to fix a specific bug at the time, with no single place showing how they compose. Two review questions during the ADR-034 PR made this gap concrete:

1. *"What's the actual pipeline from raw hardware signal to what's on screen?"* — no diagram existed.
2. *"Doesn't waiting-for-reconnect conflate command execution with transport recovery?"* — this surfaced a real design rule (below) that had been followed by instinct in some places (`power_on()`) and would have been violated by the naive "fix" first proposed for others (a `power_off()` that blocks on its own downstream reconnect). The rule needed to be written down so future code doesn't re-violate it.

This ADR is that map, plus the two ownership rules implicit in the code as it stands today.

## Decision

### The pipeline

```
┌─────────────────────────────────────────────────────────────────────┐
│ 1. RAW SIGNALS  (hardware / OS / subprocess — one fact, one source)  │
├─────────────────────────────────────────────────────────────────────┤
│ • BLE GATT connect/disconnect callbacks       (bleak/BlueZ D-Bus)    │
│ • Vendor protocol probe replies/timeouts      (battery 0x9D/0x9E,    │
│                                                 firmware 0x21/0x22)  │
│ • A2DP (BT Classic) link state                (bluetoothctl via     │
│                                                 _a2dp_connect.py)    │
│ • librespot subprocess exit/crash             (SpotifyService)      │
│ • NetworkManager WiFi state                   (ProvisioningService) │
│ • BlueZ D-Bus pairing/scan events             (PairingService)      │
│ • browser ↔ Companion fetch/WebSocket success │ (Portal, client-side)│
└──────────────────────────────┬────────────────────────────────────┘
                                │  one component owns each fact —
                                │  nothing downstream re-derives it
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 2. DAEMON-DERIVED STATE  (partyboxd / companion — the only place    │
│    a raw signal is turned into a named fact)                        │
├─────────────────────────────────────────────────────────────────────┤
│ • StatusSnapshot                  (partyboxd/device/manager.py)     │
│     .connected, .speaker_state (ADR-033), .has_battery, .battery,   │
│     .speaker_awake                                                  │
│ • AudioService.audio_ready / AudioStatus        (ADR-026, ADR-028)  │
│ • SpotifyService status (running/active)                            │
│ • PairingService pairing_state (idle/scanning/pairing/failed)        │
│ • ProvisioningService wifi status (unprovisioned/ap_active/          │
│   connecting/connected)                                              │
│ • Supervisor.health() — TaskHealth per supervised coroutine          │
│   (computed, not yet exposed via API — portal-redesign.md §13 step 3)│
└──────────────────────────────┬────────────────────────────────────┘
                                │  exposed two ways:
                                │
                    ┌───────────┴────────────┐
                    ▼                        ▼
        WS PUSH (event-driven,      REST POLL (GET /api/v1/health,
        near-instant, itemized):    /speaker, /battery, /audio,
        connected, disconnected,    /spotify, /config, /wifi/status —
        speaker_state_changed,      15s reconcile safety net, matching
        power_changed,              the daemon's own 15s health-check
        volume_changed, ping        cadence, ADR-028's reasoning)
                    │                        │
                    └───────────┬────────────┘
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 3. PORTAL VIEW STATE  (the `S` object, webui/static/index.html)      │
├─────────────────────────────────────────────────────────────────────┤
│ • S.health, S.speaker, S.battery, S.spotify, S.audio, S.wifi, S.config│
│     — pass-through copies of the daemon-derived state above;         │
│       the Portal never re-infers a fact these already carry          │
│       (this is what ADR-033 fixed: on/standby used to be re-guessed  │
│       client-side from raw battery readings — a duplicate, buggier   │
│       copy of a fact the daemon already owned)                       │
│ • S.fetchFailed  — derived locally: did the last refresh() reach     │
│     Companion at all (portal-reachability, not device state)         │
│ • S.powerPending — the ONE fact with no daemon source at all (see    │
│     "Local-only state" below)                                        │
└──────────────────────────────┬────────────────────────────────────┘
                                │  deriveScene() — fixed precedence,
                                │  see below
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 4. RENDERED SCENE  (exactly one at a time — mountScene/patchScene)   │
├─────────────────────────────────────────────────────────────────────┤
│ BOOT → UNREACHABLE → SETUP → PAIR → OFF/POWERING → STANDBY → ON      │
│                                                                       │
│ Sheets (Settings, Health) are overlays orthogonal to scene identity — │
│ they can open on top of any scene and don't participate in the       │
│ precedence below.                                                    │
└─────────────────────────────────────────────────────────────────────┘
```

### `deriveScene()`'s precedence is itself part of the contract

The redesign's central claim ("the page is the status," `docs/design/portal-redesign.md`) only holds if exactly one scene can ever be true for a given `S`. That depends on an ordered, exhaustive precedence list, not on each condition being independently correct:

1. `fetchFailed && !health` → `UNREACHABLE` (can't reach Companion at all)
2. `!health` → `BOOT` (first load, no data yet)
3. `speaker_state === "off"` → `POWERING` if `powerPending` is set, else `OFF`
4. `!audio.address` → `PAIR` (speaker never paired)
5. `speaker_state === "standby"` → `STANDBY`
6. else → `ON`

Adding a new signal to the Portal means deciding where it slots into *this* list, not just adding an independent `if`.

### Rule 1 — single ownership

For any given fact, exactly one component computes it authoritatively. Every layer below that either passes the fact through unchanged or re-projects it into presentation shape — it never re-derives the same fact from more primitive signals a second time. `speaker_state` is decided once, in `DeviceManager` (ADR-033); the Portal reads it, it does not recompute it. The pre-ADR-033 bug (inferring on/standby client-side from raw battery values, defaulting to "on" before any reading existed) was exactly a violation of this rule, in the days before it was written down.

### Rule 2 — command outcome ≠ transport recovery

A REST endpoint that performs a mutating command reports **only** whether that command's own operation succeeded. Any downstream, eventually-consistent effect (a BLE reconnect, an A2DP re-negotiation, a DHCP lease) is separately observable through the state/event stream in layer 2 — never folded into the mutating call's status code.

This is why `power_off()` (ADR-034) returns as soon as its write succeeds and does **not** wait for the BLE reconnect its own command triggers: that reconnect is a fact about layer 2 (`speaker_state`, pushed via `speaker_state_changed`), not a fact about whether the power-off command executed. A proposed "fix" during the ADR-034 review — making `power_off()` block on and report the subsequent reconnect — was rejected specifically because it would violate this rule: a speaker that successfully powered off would report as an API failure merely because the daemon's own connection bookkeeping hadn't caught up yet. `power_on()`'s wait (`_get_connected_device()`) does *not* violate this rule, because there the wait is a precondition to the one operation (nothing has executed yet when it starts waiting) — not a second, separately-outcomed operation being folded into the first.

### Local-only state: `powerPending` is the one deliberate exception

Every other fact in layer 3 has a daemon source. `S.powerPending` does not — it exists purely because `speaker_state: "off"` is genuinely ambiguous between "the speaker is unplugged" and "a power command we just sent is mid-reconnect" (ADR-034), and no daemon signal currently distinguishes the two from the Portal's vantage point. It is bridged, not owned: cleared by the real signal (`speaker_state` moving off `"off"`, via the existing WS event) whenever that arrives, with a 22s timer purely as a dead-man's switch for the case where reconnect never happens at all — not as the mechanism that decides "is it done." Any future signal added to the Portal should default to layer-2-sourced; a new local-only flag should be treated as exceptional and justified the same way this one is.

## Consequences

**Benefits:**
- One reference point for "where does fact X come from and who else reads it" — useful both for onboarding and for catching a Rule-1/Rule-2 violation in review before it ships, the way the ADR-034 review caught one before it was written.
- Makes the Portal's core correctness property (`deriveScene()` never shows contradictory state) auditable: it now visibly depends on the precedence list being exhaustive, not on each `if` being independently reasoned about.

**Accepted trade-offs:**
- This is a living document, not generated from code — it will silently drift if a new top-level state category (a new WS event type, a new top-level `S` field, a new scene) is added without updating it. Whoever adds one should treat updating this ADR's diagram as part of that change, the same way a new capability updates ADR-006's list.

## Rejected alternatives

- **A runtime-introspectable state graph/schema in code**, instead of a written document — rejected as premature machinery. This is worth doing if a second daemon or a second frontend client ever needs to agree on this contract programmatically; with exactly one daemon process and one Portal, a document that a human keeps in sync is proportionate.

Related: [ADR-006](006-capability-model.md), [ADR-026](026-bluetooth-audio-pairing.md), [ADR-028](028-audio-readiness-model.md), [ADR-033](033-speaker-standby-detection.md), [ADR-034](034-power-command-reconnect-wait.md) (Rule 2 is this ADR's generalization of that ADR's `power_off()`/`power_on()` discussion).
