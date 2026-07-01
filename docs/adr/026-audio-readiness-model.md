# ADR-026 — Audio Readiness Model

**Status:** Accepted
**Date:** 2026-06-30
**Milestone:** M17.2

---

## Context

Before M17.2, the Companion Portal reported appliance health by asking one
question: is BLE GATT connected?  If `DeviceManager` had a live BLE link to the
speaker, the Portal showed "Connected" and considered the appliance healthy.

Hardware testing revealed the flaw in this model.  BLE GATT (control) and
Bluetooth Classic A2DP (audio) are completely independent Bluetooth subsystems.
A BLE connection confirms that the control channel works.  It says nothing about
whether audio can actually be produced.  The appliance could show "Connected"
while the A2DP sink is absent, meaning no audio would reach the speaker at all.

This is the false-positive health problem: **the appliance appears ready while
a critical subsystem is not**.

---

## The `audio_ready` concept

`audio_ready` is a boolean owned by `AudioService` that answers a different
question from BLE connectivity:

> **Is the appliance currently capable of producing audio?**

For the current hardware, `audio_ready` is `True` when the Bluetooth Classic
A2DP link to the speaker is established.  The definition may be extended in the
future — for example to require a working PipeWire sink or A2DP codec
negotiation — without changing the interface that consumers see.

`audio_ready` is not the same as `ble_connected`:

| State | `ble_connected` | `audio_ready` | Meaning |
|-------|-----------------|---------------|---------|
| BLE only | ✓ | ✗ | Control works; audio does not |
| A2DP only | ✗ | ✓ | Audio works; BLE control unavailable |
| Both | ✓ | ✓ | Fully operational |
| Neither | ✗ | ✗ | Appliance is reconnecting |

The normal operating state is "both connected." The other combinations are
transient states seen during startup and recovery.

---

## Why `AudioService` owns `audio_ready`

`AudioService` is the component that understands what A2DP connectivity means
and what it takes to establish and verify it.  No other component should need to
inspect the internals of Bluetooth Classic to answer the "can we produce audio"
question.

Other services — Spotify, AirPlay, future playback services — should react to
`audio_ready` without knowing that A2DP is the mechanism.  If the underlying
transport changes (e.g. USB audio), `AudioService` can change its internal
implementation while consumers see the same `audio_ready` interface.

---

## Event-driven publication

`AudioService` publishes `AudioReadyChanged` events on every `audio_ready`
transition via a subscription bus modelled on the same pattern as
`DeviceManager`'s `EventBus`.  No event is emitted when state is unchanged.

### Subscribe contract

`subscribe()` immediately enqueues the **current state** as the first item in
the returned queue, then delivers all future transitions:

```python
queue = audio.subscribe()   # first item is AudioReadyChanged(audio_ready=<now>)
while True:
    event = await queue.get()   # AudioReadyChanged(audio_ready=True/False)
    ...
audio.unsubscribe(queue)
```

This BehaviorSubject pattern eliminates the late-joiner problem: a consumer
subscribing at any point in time gets the current state immediately and can
start acting without waiting for the next transition.  The pattern also avoids
a TOCTOU race: if the caller read `audio_ready` separately before subscribing,
a transition occurring between those two steps would be lost.  Processing only
the subscription queue is sufficient and race-free.

Events are broadcast to all current subscribers.  Slow subscribers have events
dropped silently rather than stalling `AudioService`.

### Reliability guarantee

`_set_audio_ready()` is synchronous — it calls `put_nowait()` without any
`await`.  In a single asyncio event loop, no other coroutine can preempt a
synchronous call; the event is in every subscriber's queue before anything
else can run.  Between any two `_set_audio_ready()` calls there is always at
least one `await` point (`_is_connected()`, `_connect()`, or
`asyncio.sleep()`), giving subscribers time to drain.

Audio readiness changes at human timescales (seconds to minutes), not thousands
of times per second, so the 64-slot queue capacity is far more than sufficient
under normal operation.  The only event-loss case is a subscriber whose
coroutine never awaits the queue and lets it fill; that pathological case is
intentionally handled by dropping rather than blocking the emitter.

This design allows multiple consumers to react independently:

- **M17.3**: `SpotifyService` subscribes and starts/stops librespot in response
- **Future**: AirPlay service subscribes and manages shairport-sync
- **Future**: Portal diagnostics subscribe for real-time status

`AudioService` does not know about any of its subscribers.  Subscribers do not
know about Bluetooth internals.

---

## Health API

`GET /api/v1/health` now reports both fields separately:

```json
{
    "status": "ok",
    "version": "0.1.0-dev",
    "ble_connected": true,
    "audio_ready": false
}
```

`ble_connected` is the BLE GATT connection state owned by `DeviceManager`.
`audio_ready` is polled from `AudioService` via a `Callable[[], bool]` injected
into `make_router` at construction time.  When partyboxd runs standalone (without
Companion), `audio_ready` is `null`.

The field was previously named `speaker_connected`, which was ambiguous — it
implied only that the speaker device was reachable, not that audio was functional.
`ble_connected` is explicit about the transport.

---

## Event type design

`AudioReadyChanged` carries a single boolean.  A richer enum (`reconnecting`,
`degraded`, etc.) was considered and rejected:

- Consumers answer a binary question: "should I be running?"  Introducing
  intermediate states forces every consumer to map them back to a binary
  decision, embedding Bluetooth knowledge where it does not belong.
- `reconnecting` is an `AudioService` implementation detail.  During
  reconnection the correct signal to emit is `audio_ready = False`: do not
  route audio here right now.
- A `degraded` state would require `AudioService` to understand PipeWire or
  codec negotiation — coupling that belongs in a separate layer.
- Starting with a binary is the safest migration path: adding enum values later
  is backward-compatible; removing them is not.

If richer diagnostic context is needed in the future, the event can be extended
with an optional field:
```python
AudioReadyChanged(audio_ready: bool, details: str | None = None)
```
without breaking the boolean semantics that existing consumers depend on.

---

## M17.3 — Spotify audio gate

M17.3 implements Spotify lifecycle gating via `_gate_spotify_on_audio` in
`companion/__main__.py`.  The gate is registered with the Supervisor as
`spotify-audio-gate`, replacing the previous direct `spotify-service`
registration.

```
audio_ready == True  → start librespot → Spotify Connect visible
audio_ready == False → wait grace period → stop librespot → Connect invisible
```

### State machine

```
IDLE (no spotify_task)
  + AudioReadyChanged(True)   → RUNNING: create spotify_task
  + AudioReadyChanged(False)  → no-op

RUNNING (spotify_task exists)
  + AudioReadyChanged(True)   → no-op (already running)
  + AudioReadyChanged(False)  → GRACE: wait _AUDIO_GRACE_SECONDS

GRACE (spotify_task exists, timer running)
  + AudioReadyChanged(True) within grace  → RUNNING: cancel timer, keep task
  + timeout                               → IDLE: cancel spotify_task
```

### Grace period

`_AUDIO_GRACE_SECONDS = 60.0`.  `AudioService` polls A2DP every 30 s when
connected; a transient drop and reconnect takes approximately 30 s (check
interval) + 10 s (retry delay) + 15 s (connect attempt) = ~55 s.  A 60 s grace
period covers most transient blips while ensuring Spotify de-registers within
~90 s of a real speaker power-off (30 s detection + 60 s grace).

### Responsibility model

- `SpotifyService.run()` owns librespot subprocess crash-recovery internally.
  Its contract: runs until cancelled; handles librespot exits; only exits via
  `CancelledError`.  The gate does not supervise it.
- `_gate_spotify_on_audio` owns start/stop based on `audio_ready`.  If
  `spotify.run()` exits other than by cancellation that is a violated
  invariant; the gate propagates so the Supervisor can restart it and the
  BehaviorSubject subscription restores the correct state immediately.
- `Supervisor` owns the gate coroutine (`spotify-audio-gate`).

### Package placement

The gate lives in `companion/__main__.py`, the orchestration entry point,
alongside the analogous `_forward_ble_volume` coroutine.  Neither `AudioService`
nor `SpotifyService` knows about the other; the gate is the only coupling point.

---

## Consequences

### What changes

- `AudioService.audio_ready` property is the canonical answer to "can we play
  audio" — consumers use this instead of inspecting BLE state.
- `GET /api/v1/health` returns `ble_connected` and `audio_ready` as distinct
  fields, enabling external monitors to distinguish the two layers.
- `AudioService.subscribe()` / `unsubscribe()` provide the event bus seam for
  M17.3 and later consumers.
- `AudioStatus.connected` (existing field, used by `GET /api/v1/audio`) continues
  to reflect `audio_ready` for backward compatibility with Portal polling.

### What does not change

- `AudioService` still uses `bluetoothctl` polling to detect A2DP state.  The
  polling interval (30 s when connected, shorter when reconnecting) is an
  implementation detail that can be improved independently.
- The Portal's Bluetooth Audio row already shows A2DP state correctly and does
  not require changes to display `audio_ready` accurately.
- BLE GATT reconnection is still handled entirely by `DeviceManager`.

### Hardware validation scenarios

The following scenarios should be validated on-device. All are handled
automatically by the existing `AudioService` reconnect loop and the
initial-state delivery from `subscribe()`.

| Scenario | Expected behaviour |
|----------|--------------------|
| Boot with speaker already on | `_is_connected()` returns `True` on first poll; `audio_ready` → `True` within 5 s |
| Boot with speaker off | `audio_ready` stays `False`; transitions to `True` when speaker turns on and A2DP reconnects |
| Speaker powered off during operation | `_is_connected()` returns `False` on next poll (≤ 30 s); `AudioReadyChanged(False)` emitted |
| Speaker powered back on | A2DP reconnects; `AudioReadyChanged(True)` emitted |
| Temporary out-of-range | Same as powered off; recovers automatically when back in range |
| Repeated reconnect cycles | Exponential backoff prevents radio hammering; each reconnect emits correct events |
| **Process restart with speaker connected** | `AudioService` always initialises `_audio_ready = False`. The first `_is_connected()` poll (≤ 5 s) rediscovers the live A2DP connection and emits `AudioReadyChanged(True)`. The gate receives the current state immediately from `subscribe()` and starts librespot once `True` arrives. No manual intervention required after `systemctl restart partybox-companion`. |
| **Speaker on → Spotify visible** | Gate receives `AudioReadyChanged(True)`; starts librespot; Spotify Connect device appears in clients. |
| **Speaker off → Spotify hidden after grace** | Gate receives `AudioReadyChanged(False)`; waits 60 s; cancels librespot; Spotify Connect device disappears from clients. |
| **Transient A2DP drop → Spotify unaffected** | Gate receives `AudioReadyChanged(False)`, starts 60 s grace; `AudioReadyChanged(True)` arrives within grace; librespot continues running without interruption. |

### Deferred

- Reducing the A2DP health-check interval below 30 s: useful for faster
  disconnect detection, but the event bus means consumers react immediately
  when a change IS detected.  The polling interval is a separate tuning
  concern.
- BlueZ D-Bus events as an alternative to `bluetoothctl` polling: would
  allow near-instant disconnect detection; deferred as a significant
  integration effort.
- Portal real-time `audio_ready` updates via WebSocket: currently the Portal
  reflects audio_ready through the existing `/api/v1/audio` poll.  A
  WebSocket `audio_ready_changed` event would make the Portal fully
  push-based; deferred to M17.4 or later.
