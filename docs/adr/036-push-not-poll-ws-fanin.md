# ADR-036: Push, Don't Poll — a Merged WebSocket Stream Across partyboxd and companion

**Status:** Accepted

---

## Context

The Portal redesign's roadmap (`docs/design/portal-redesign.md` §12, item 2) called for replacing the 2s pairing poll and slow reconciliation poll with push events, once the daemon could emit them — deferred at the time because only the BLE side (`speaker_state`, `connected`/`disconnected`) had push events at all ([ADR-033](033-speaker-standby-detection.md), [ADR-034](034-power-command-reconnect-wait.md)). Bluetooth Classic audio (`AudioService`), Spotify Connect (`SpotifyService`), and Bluetooth pairing (`PairingService`) were still poll-only: the Portal hit `GET /api/v1/audio` every 2 seconds during an active pairing attempt, and folded the rest into the general 15s reconciliation poll ([ADR-035](035-state-ownership-and-signal-pipeline.md)'s pipeline diagram, written just before this change, documents that pre-push state).

Two structural facts made this tractable now:

- `AudioService` already had its own `subscribe()`/`unsubscribe()` pub-sub (an `_AudioEventBus`, ADR-026) for internal use (gating `SpotifyService` on audio readiness). `DeviceManager` had an equivalent, independently-written `EventBus` for the same purpose. Adding push events to `SpotifyService` and `PairingService` would have meant writing that same broadcast-dispatcher class a third and fourth time.
- partyboxd's WebSocket endpoint (`GET /api/v1/events`) already existed and was hard-wired to `DeviceManager`'s bus alone. `SpotifyService`/`PairingService`/`AudioService` live one layer up, in `companion` — and per the project's layering (`partybox → partyboxd → companion`, ADR-004/005), partyboxd must not import companion's event types.

## Decision

### 1. Generalize `EventBus` once, at the third/fourth instance

Extracted the broadcast-dispatcher pattern (independently written twice already, in `DeviceManager` and `AudioService`) into a single generic `partyboxd.eventbus.EventBus[T]`. `DeviceManager`, `AudioService`, and the newly-instrumented `SpotifyService`/`PairingService` all now construct `EventBus[TheirEventType]()` rather than each hand-rolling the same subscribe/unsubscribe/emit logic. This follows the project's existing rule of thumb for generalizing narrowly-scoped helpers (ADR-032: introduce the shared abstraction "before the third or fourth [instance] lands, not before") — this is that third-and-fourth instance.

`EventBus` lives in `partyboxd` (not `companion`) specifically so companion can depend on it — the dependency only ever points one way.

### 2. `SpotifyService` and `PairingService` get `subscribe()`/`unsubscribe()`

Mirroring `AudioService`'s existing shape exactly:

- `SpotifyService.subscribe()` returns a queue pre-populated with the current `SpotifyStatusChanged` (running/active/device_name), then delivers one on every running/active transition. A new `_set_status()` helper centralizes what were previously several direct `self._running = ...` / `self._active = ...` assignments scattered across `_run_once`, `_infer_playback_state`, and `_terminate`, so no transition can be missed by a future edit.
- `PairingService.subscribe()` returns a queue pre-populated with the current `PairingProgressEvent` (state/error), then one per state transition. A `_set_state()` helper replaces the equivalent scattered assignments in `_do_pair()`.

Both follow [ADR-035](035-state-ownership-and-signal-pipeline.md)'s Rule 1 (single ownership) — the running/active or state/error pair is set in exactly one place now, not several.

### 3. partyboxd's WS endpoint accepts additional generic event sources

`make_ws_router`/`create_app` gained `extra_sources: Sequence[EventSource]`, where `EventSource` is a minimal structural `Protocol` (`subscribe() -> Queue[Any]`, `unsubscribe(queue)`) — satisfied by `DeviceManager` and by any `EventBus`-backed service without partyboxd importing a single companion type. `companion/__main__.py` passes `[audio, spotify, pairing]` when constructing the app.

Internally, each source gets one lightweight `_forward()` task that relays its queue into a single merged queue; the WS handler's main loop reads only that one merged queue (unchanged from before, apart from the source). This was chosen over recreating an `asyncio.wait()` over N per-iteration tasks each loop tick — one persistent forwarder per source is simpler to reason about and cheaper (no repeated task setup/teardown for sources that rarely emit).

### 4. New event types carry enough payload to update Portal state directly

`audio_changed` (`audio_ready`, `address`), `spotify_changed` (`running`, `active`, `device_name`), `pairing_progress` (`state`, `error`) — each one lets the Portal's `onEvent()` update `S.audio`/`S.spotify` directly from the event payload and re-derive the scene, with **no follow-up REST call**. This is the actual point of push over poll: not just faster notification that something changed, but skipping the round-trip entirely.

### 5. The Portal drops the 2s pairing poll; the general poll relaxes to 30s, safety-net only

`startPairPoll()` (2s `setInterval` against `GET /api/v1/audio`) is gone. `startPairing()` now sets a local `S.pairingActive` flag and a **180s** `setTimeout` safety net (matching the old poll's 90×2s give-up point); progress comes from `pairing_progress`/`audio_changed` WS events via `handlePairingProgress()`. This is the same shape as [ADR-034](034-power-command-reconnect-wait.md)'s `powerPending`: a client-local flag with no daemon source, bridging a real gap, cleared by the real signal when it arrives and by a bounded timer only as a last resort — see [ADR-035](035-state-ownership-and-signal-pipeline.md)'s "local-only state" rule.

The general reconciliation poll (`startReconcilePoll`) goes from 15s to 30s. It was already the safety net, never the primary update path for anything WS now covers; slowing it further only affects how quickly a *missed* event or a dropped-then-reconnected WS session catches up, not normal-path latency.

**Validated on hardware:** connected to the live `/api/v1/events` stream directly and observed `audio_changed`/`spotify_changed`/`pairing_progress` delivered on connect (seeded current state) and on real transitions (`audio_ready: false → true`, `spotify.running: false → true`) merged correctly alongside the pre-existing BLE-side events and heartbeat pings, over the same single connection.

## Consequences

**Benefits:**
- Pairing progress, Spotify status, and audio connectivity now reach the Portal in the same tick they happen, not up to 2s (pairing) or up to 30s (general poll) later.
- Removes a periodic subprocess-spawning `GET /api/v1/audio` call every 2 seconds for the duration of every pairing attempt (each `/api/v1/audio` fetch on the companion side may shell out to check A2DP status — see `AudioService._is_connected()`), reducing avoidable Bluetooth/D-Bus/subprocess chatter during exactly the window (active pairing) where the radio is already under the most pressure.
- No REST API surface changed — `GET /api/v1/audio`, `/spotify`, etc. still exist and still work for clients that don't use the WS stream (ADR-012 interoperability).

**Accepted trade-offs:**
- The Portal's near-real-time pairing feedback now depends on the WS connection specifically. If it drops mid-pairing, `ws.onclose`'s 5s reconnect plus the 30s reconciliation poll are the fallback — pairing was already a bounded, user-attended flow (with its own 180s give-up), so this is a acceptable degradation, not a silent one (the `S.pairingActive` timer still fires the "timed out" message even if no more events arrive at all).
- `EventSource`/`EventBus` add a small amount of structural ceremony (subscribe/unsubscribe symmetry, pre-populating the "current state" event) that any future companion service wanting push events must repeat — already an accepted cost for `AudioService` since ADR-026; this ADR just formalizes it as the pattern for new services too, rather than each reinventing it.

## Rejected alternatives

- **A second, companion-owned WebSocket endpoint** for audio/spotify/pairing events, leaving partyboxd's `/api/v1/events` untouched. Rejected: the Portal would need to manage two WS connection lifecycles (reconnect, auth, heartbeat) instead of one, for no benefit — nothing about these events requires a separate channel, and the design doc calls for one stream.
- **Recreate `asyncio.wait()` over all source queues every loop iteration**, instead of persistent per-source forwarder tasks feeding one merged queue. Rejected as more complex for no benefit: N tasks recreated every tick vs. N tasks created once per WS connection and left idling; the merged-queue approach is also easier to reason about (the main loop's logic is unchanged from before this ADR — it still just reads one queue).
- **Let companion own its own copy of `EventBus`** rather than sharing partyboxd's generic one. Rejected: would leave the exact duplication (now four services, not two) that motivated generalizing it in the first place, and companion already depends downward on partyboxd for other types (`SpeakerStateChangedEvent`, etc.) — adding `EventBus` to that existing dependency is free.

Related: [ADR-026](026-bluetooth-audio-pairing.md) (the original `AudioService` subscribe/bus this generalizes), [ADR-028](028-audio-readiness-model.md), [ADR-032](032-capability-probing.md) (the "generalize at the third/fourth instance" precedent this follows), [ADR-034](034-power-command-reconnect-wait.md) (`powerPending`, the local-only-flag pattern `pairingActive` mirrors), [ADR-035](035-state-ownership-and-signal-pipeline.md) (the signal-ownership rules this realizes, and whose pipeline diagram this ADR's changes update).
