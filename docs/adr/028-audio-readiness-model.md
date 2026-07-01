# ADR-028 — Audio Readiness Model and A2DP Connection Management

**Status:** Accepted
**Date:** 2026-06-30 (M17.2) / 2026-07-01 (M17.4)
**Milestones:** M17.2, M17.4
**Extends:** [ADR-026](026-bluetooth-audio-pairing.md) — the two-state model, REST surface, and Portal UX established there are unchanged; this ADR defines how `audio_ready` is abstracted, published, and kept alive at runtime.

---

## Context

Before M17.2, the Companion Portal reported appliance health by asking one question: is BLE GATT connected? If `DeviceManager` had a live BLE link to the speaker, the Portal showed "Connected" and considered the appliance healthy.

Hardware testing revealed the flaw in this model. BLE GATT (control) and Bluetooth Classic A2DP (audio) are completely independent Bluetooth subsystems. A BLE connection confirms that the control channel works. It says nothing about whether audio can actually be produced. The appliance could show "Connected" while the A2DP sink is absent, meaning no audio would reach the speaker at all.

This is the false-positive health problem: **the appliance appears ready while a critical subsystem is not**.

A2DP connection management turned out to be harder than expected. M17.4 appliance validation uncovered two failure modes that required architectural decisions: a routing conflict between `dbus-fast` and `bleak` that made all BlueZ D-Bus calls from `AudioService` silently unreliable, and a WirePlumber endpoint degradation that caused sustained `profile-unavailable` failures after approximately one hour of idle disconnect/reconnect cycles.

---

## The `audio_ready` concept

`audio_ready` is a boolean owned by `AudioService` that answers a different question from BLE connectivity:

> **Is the appliance currently capable of producing audio?**

For the current hardware, `audio_ready` is `True` when the Bluetooth Classic A2DP link to the speaker is established. The definition may be extended in the future — for example to require a working PipeWire sink or A2DP codec negotiation — without changing the interface that consumers see.

`audio_ready` is not the same as `ble_connected`:

| State | `ble_connected` | `audio_ready` | Meaning |
|-------|-----------------|---------------|---------|
| BLE only | ✓ | ✗ | Control works; audio does not |
| A2DP only | ✗ | ✓ | Audio works; BLE control unavailable |
| Both | ✓ | ✓ | Fully operational |
| Neither | ✗ | ✗ | Appliance is reconnecting |

The normal operating state is "both connected." The other combinations are transient states seen during startup and recovery.

---

## Why `AudioService` owns `audio_ready`

`AudioService` is the component that understands what A2DP connectivity means and what it takes to establish and verify it. No other component should need to inspect the internals of Bluetooth Classic to answer the "can we produce audio" question.

Other services — Spotify, AirPlay, future playback services — should react to `audio_ready` without knowing that A2DP is the mechanism. If the underlying transport changes (e.g. USB audio), `AudioService` can change its internal implementation while consumers see the same `audio_ready` interface.

---

## Event-driven publication

`AudioService` publishes `AudioReadyChanged` events on every `audio_ready` transition via a subscription bus modelled on the same pattern as `DeviceManager`'s `EventBus`. No event is emitted when state is unchanged.

### Subscribe contract

`subscribe()` immediately enqueues the **current state** as the first item in the returned queue, then delivers all future transitions:

```python
queue = audio.subscribe()   # first item is AudioReadyChanged(audio_ready=<now>)
while True:
    event = await queue.get()   # AudioReadyChanged(audio_ready=True/False)
    ...
audio.unsubscribe(queue)
```

This BehaviorSubject pattern eliminates the late-joiner problem: a consumer subscribing at any point in time gets the current state immediately and can start acting without waiting for the next transition. The pattern also avoids a TOCTOU race: if the caller read `audio_ready` separately before subscribing, a transition occurring between those two steps would be lost. Processing only the subscription queue is sufficient and race-free.

Events are broadcast to all current subscribers. Slow subscribers have events dropped silently rather than stalling `AudioService`.

### Reliability guarantee

`_set_audio_ready()` is synchronous — it calls `put_nowait()` without any `await`. In a single asyncio event loop, no other coroutine can preempt a synchronous call; the event is in every subscriber's queue before anything else can run. Between any two `_set_audio_ready()` calls there is always at least one `await` point (`_is_connected()`, `_connect()`, or `asyncio.sleep()`), giving subscribers time to drain.

Audio readiness changes at human timescales (seconds to minutes), not thousands of times per second, so the 64-slot queue capacity is far more than sufficient under normal operation. The only event-loss case is a subscriber whose coroutine never awaits the queue and lets it fill; that pathological case is intentionally handled by dropping rather than blocking the emitter.

This design allows multiple consumers to react independently:

- **M17.3**: `SpotifyService` subscribes and starts/stops librespot in response
- **Future**: AirPlay service subscribes and manages shairport-sync
- **Future**: Portal diagnostics subscribe for real-time status

`AudioService` does not know about any of its subscribers. Subscribers do not know about Bluetooth internals.

---

## Health API

`GET /api/v1/health` reports both fields separately:

```json
{
    "status": "ok",
    "version": "0.1.0-dev",
    "ble_connected": true,
    "audio_ready": false
}
```

`ble_connected` is the BLE GATT connection state owned by `DeviceManager`. `audio_ready` is polled from `AudioService` via a `Callable[[], bool]` injected into `make_router` at construction time. When partyboxd runs standalone (without Companion), `audio_ready` is `null`.

The field was previously named `speaker_connected`, which was ambiguous — it implied only that the speaker device was reachable, not that audio was functional. `ble_connected` is explicit about the transport.

---

## Event type design

`AudioReadyChanged` carries a single boolean. A richer enum (`reconnecting`, `degraded`, etc.) was considered and rejected:

- Consumers answer a binary question: "should I be running?" Introducing intermediate states forces every consumer to map them back to a binary decision, embedding Bluetooth knowledge where it does not belong.
- `reconnecting` is an `AudioService` implementation detail. During reconnection the correct signal to emit is `audio_ready = False`: do not route audio here right now.
- A `degraded` state would require `AudioService` to understand PipeWire or codec negotiation — coupling that belongs in a separate layer.
- Starting with a binary is the safest migration path: adding enum values later is backward-compatible; removing them is not.

If richer diagnostic context is needed in the future, the event can be extended with an optional field:
```python
AudioReadyChanged(audio_ready: bool, details: str | None = None)
```
without breaking the boolean semantics that existing consumers depend on.

---

## M17.3 — Spotify audio gate

M17.3 implements Spotify lifecycle gating via `_gate_spotify_on_audio` in `companion/__main__.py`. The gate is registered with the Supervisor as `spotify-audio-gate`, replacing the previous direct `spotify-service` registration.

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

`_AUDIO_GRACE_SECONDS = 300.0`. A2DP health checks run every 60 s when connected; a transient drop and reconnect takes approximately 60 s (check interval) + 10 s (retry delay) + 15 s (connect attempt) = ~85 s. The 300 s grace period covers transient drops and slow speaker wake-ups, while ensuring Spotify deregisters within ~360 s of a real speaker power-off that does not recover. This was 60 s in M17.2 — increased in M17.4 after validation showed the 60 s window left too little margin for real-world speaker behaviour.

### Responsibility model

- `SpotifyService.run()` owns librespot subprocess crash-recovery internally. Its contract: runs until cancelled; handles librespot exits; only exits via `CancelledError`. The gate does not supervise it.
- `_gate_spotify_on_audio` owns start/stop based on `audio_ready`. If `spotify.run()` exits other than by cancellation that is a violated invariant; the gate propagates so the Supervisor can restart it and the BehaviorSubject subscription restores the correct state immediately.
- `Supervisor` owns the gate coroutine (`spotify-audio-gate`).

### Package placement

The gate lives in `companion/__main__.py`, the orchestration entry point, alongside the analogous `_forward_ble_volume` coroutine. Neither `AudioService` nor `SpotifyService` knows about the other; the gate is the only coupling point.

---

## M17.4 — A2DP Connection Management

### BlueZ D-Bus subprocess isolation

**Problem:** `AudioService` originally ran BlueZ D-Bus calls (`ConnectProfile`, `Device1.Connected`) in the same asyncio event loop as `bleak` (used by `DeviceManager` for BLE GATT — [ADR-015](015-bluetooth-control-transport.md)). `bleak` creates its own `dbus-fast` `MessageBus` in the companion process's event loop. Any second `MessageBus` created in the same loop misroutes D-Bus responses — replies destined for one bus arrive on the other. This caused both `_connect()` and `_is_connected()` to silently return wrong values: no exception was raised, but no A2DP sink was actually established in PipeWire.

**Decision:** All BlueZ D-Bus calls originating in `AudioService` run in isolated subprocesses via `companion.services._a2dp_connect`. This module is invoked as `python -m companion.services._a2dp_connect <MAC> [connect|check]` and runs its own `asyncio.run()` event loop with a single `MessageBus` — no routing conflict is possible. The subprocess prints `"ok"` / `"err:<msg>"` (connect) or `"true"` / `"false"` (check) to stdout; `AudioService` reads these via `asyncio.create_subprocess_exec()` with a stdout pipe.

`PairingService` ([ADR-027](027-bluetooth-bonding-architecture.md)) is not affected — it creates its `MessageBus` during a distinct operational phase before `bleak`'s bus is active, and manages its own D-Bus connection lifecycle.

**Note on `bluez_dbus.py`:** `BluezClient.connect_a2dp()` / `disconnect_a2dp()` were added during ADR-027 work. These remain in the module but are not called by `AudioService` for the connect/check paths due to the routing conflict; `AudioService` uses the subprocess path exclusively. `_disconnect()` still calls `BluezClient.disconnect_a2dp()` because a teardown path is not timing-critical and the routing issue only manifests under concurrent bleak traffic.

**Why not a shared `MessageBus` or session broker:** The routing conflict is inherent to `dbus-fast`'s asyncio integration and cannot be fixed by configuration. The subprocess approach is the only isolation boundary that is both simple and guaranteed correct.

### WirePlumber health monitoring

**Problem:** After approximately one hour of A2DP idle disconnect/reconnect cycles, WirePlumber loses its BlueZ A2DP media-endpoint registration. All subsequent `ConnectProfile()` calls return `br-connection-profile-unavailable` — not because the speaker rejected the connection, but because BlueZ has no registered A2DP handler on the Pi. The only recovery is a WirePlumber restart. Without automated detection, the appliance silently fails to produce audio until the companion service is manually restarted.

**Key signal distinction:** `br-connection-profile-unavailable` is a **local** BlueZ error (no handler registered). When WirePlumber's endpoints are intact and the speaker is simply off or out of range, BlueZ returns `br-connection-create-failed` — a distinct error. This makes `profile-unavailable` a reliable, unambiguous local-failure signal rather than a speaker-state signal.

**Decision:** `AudioService` tracks consecutive `profile-unavailable` failures. After `_WP_RESTART_THRESHOLD = 3` consecutive failures (approximately 70 s: 10 s + 20 s + 40 s of exponential backoff), and if `_WP_RESTART_COOLDOWN = 300.0` seconds have elapsed since the last automatic restart, `AudioService` runs:

```
sudo systemctl --user -M pi@ restart wireplumber
```

This restarts WirePlumber in the `pi` user session (where PipeWire runs), re-registering its BlueZ A2DP media endpoints. `AudioService` then sleeps 10 seconds to allow endpoint registration to complete before retrying.

The `companion` system user (which runs the service) is granted passwordless sudo for exactly this command via a dedicated sudoers fragment written by `install.sh`:

```
companion ALL=(root) NOPASSWD: /usr/bin/systemctl --user -M pi@ restart wireplumber
```

The threshold of 3 failures was chosen because `profile-unavailable` is an unambiguous local-failure signal — it cannot be confused with speaker-side rejection — so an aggressive recovery threshold carries no risk of false-positive restarts. Earlier drafts used a threshold of 10–15 (~8–15 minutes), rejected as unacceptably slow given the signal reliability.

**Why embedded in `AudioService` rather than a dedicated class:** The `profile-unavailable` signal exists only inside `AudioService`'s connect loop. The state — a streak counter and a last-restart timestamp — couples directly to the loop that produces the signal. A `WirePlumberWatchdog` class would receive the streak as input rather than measuring it independently, making the class boundary artificial for three fields and one method. A `record_failure() / record_success()` watchdog class remains the preferred refactor if WirePlumber monitoring is ever needed outside `AudioService`.

### Startup hardening

`companion.service` runs the following `ExecStartPre` sequence before the Python process starts:

```ini
ExecStartPre=+/usr/bin/hciconfig hci0 reset
ExecStartPre=+/bin/chmod 755 /run/user/1000
ExecStartPre=+/bin/sleep 5
ExecStartPre=+/usr/bin/systemctl --user -M pi@ restart wireplumber
ExecStartPre=+/bin/sleep 10
```

The HCI reset ([ADR-023](023-hci-controller-reset-on-startup.md)) clears stale controller state. The `chmod 755 /run/user/1000` ensures the `companion` system user can reach the PipeWire-pulse socket in the `pi` user's XDG runtime directory (`/run/user/1000`), which `systemd-logind` creates with mode 0700 by default — inaccessible to other users. The WirePlumber restart after the HCI reset ensures A2DP media endpoints are freshly registered with BlueZ on every companion start, eliminating degraded endpoint state left over from a prior session without waiting for the in-process threshold to trigger.

The `+` prefix grants these steps transient root elevation without making the main service run as root ([ADR-018](018-systemd-service.md)).

### A2DP connection parameters (M17.4 tuning)

| Parameter | M17.2 value | M17.4 value | Reason for change |
|-----------|-------------|-------------|-------------------|
| `_CHECK_INTERVAL` | 30 s | 60 s | 30 s polling was unnecessarily aggressive for an idle-connected link; the event bus delivers state changes independently of polling, so the interval only affects detection latency for unexpected drops |
| `_AUDIO_GRACE_SECONDS` | 60 s | 300 s | The 60 s window matched the estimated transient-reconnect time but with zero margin; real-world validation showed the speaker sometimes takes longer to accept A2DP after power-on; 300 s gives users 5 minutes before Spotify deregisters |
| `_WP_RESTART_THRESHOLD` | — | 3 failures | New in M17.4 |
| `_WP_RESTART_COOLDOWN` | — | 300 s | New in M17.4 |

---

## Consequences

### What changes

- `AudioService.audio_ready` property is the canonical answer to "can we play audio" — consumers use this instead of inspecting BLE state.
- `GET /api/v1/health` returns `ble_connected` and `audio_ready` as distinct fields, enabling external monitors to distinguish the two layers.
- `AudioService.subscribe()` / `unsubscribe()` provide the event bus seam for M17.3 and later consumers.
- `AudioStatus.connected` (existing field, used by `GET /api/v1/audio`) continues to reflect `audio_ready` for backward compatibility with Portal polling.
- All BlueZ D-Bus calls from `AudioService` run in isolated subprocesses (`companion.services._a2dp_connect`) to avoid the `dbus-fast`/`bleak` routing conflict.
- WirePlumber is restarted automatically after 3 consecutive `profile-unavailable` failures (~70 s recovery) with a 5-minute cooldown.
- `companion.service` restarts WirePlumber on every start as part of the `ExecStartPre` sequence.

### What does not change

- BLE GATT reconnection is handled entirely by `DeviceManager` and is independent of `AudioService`.
- The Portal's Bluetooth Audio row already shows A2DP state correctly via the existing `/api/v1/audio` poll and does not require changes.
- `PairingService` uses a direct `MessageBus` (not subprocess) — the `dbus-fast`/`bleak` conflict does not affect it because it operates in a distinct phase before `bleak` is active.

### Hardware validation results (M17.4)

| Scenario | Result |
|----------|--------|
| Boot with speaker off → speaker powered on | A2DP connects within ~17 s of boot; Spotify starts automatically; music plays |
| Speaker power cycle during active session | Grace period maintains Spotify during outage; audio resumes within ~30 s of speaker power-on without manual intervention |
| Pi reboot | A2DP connected in ~17 s; Spotify started automatically |
| Sustained `profile-unavailable` (WirePlumber degraded) | Auto-restarted after 3 failures (~70 s); audio resumed without companion restart |

### Hardware validation scenarios

| Scenario | Expected behaviour |
|----------|--------------------|
| Boot with speaker already on | `_is_connected()` returns `True` on first poll; `audio_ready` → `True` within 65 s |
| Boot with speaker off | `audio_ready` stays `False`; transitions to `True` when speaker turns on and A2DP reconnects |
| Speaker powered off during operation | `_is_connected()` returns `False` on next poll (≤ 60 s); `AudioReadyChanged(False)` emitted |
| Speaker powered back on | A2DP reconnects; `AudioReadyChanged(True)` emitted |
| Temporary out-of-range | Same as powered off; recovers automatically when back in range |
| Repeated reconnect cycles | Exponential backoff (base 10 s, max 60 s) prevents radio hammering; each reconnect emits correct events |
| Process restart with speaker connected | `AudioService` always initialises `_audio_ready = False`. The first `_is_connected()` poll rediscovers the live A2DP connection and emits `AudioReadyChanged(True)`. The gate receives the current state immediately from `subscribe()` and starts librespot. No manual intervention required after `systemctl restart companion`. |
| Speaker on → Spotify visible | Gate receives `AudioReadyChanged(True)`; starts librespot; Spotify Connect device appears in clients |
| Speaker off → Spotify hidden after grace | Gate receives `AudioReadyChanged(False)`; waits 300 s; cancels librespot; Spotify Connect device disappears |
| Transient A2DP drop → Spotify unaffected | Gate receives `AudioReadyChanged(False)`, starts 300 s grace; `AudioReadyChanged(True)` arrives within grace; librespot continues without interruption |
| Sustained `profile-unavailable` (WirePlumber degraded) | After 3 consecutive failures (~70 s), WirePlumber is automatically restarted; A2DP reconnects; audio resumes |

### Deferred

- BlueZ D-Bus `PropertiesChanged` events as an alternative to subprocess polling for `_is_connected()`: would allow near-instant disconnect detection; blocked by the `dbus-fast`/`bleak` routing conflict until a clean shared-bus solution exists or `bleak` is replaced as the BLE transport.
- Portal real-time `audio_ready` updates via WebSocket: the Portal reflects `audio_ready` through the existing `/api/v1/audio` poll. A WebSocket `audio_ready_changed` event would make the Portal fully push-based; deferred to a future milestone.
- `WirePlumberWatchdog` class extraction: see M17.4 section reasoning. Preferred refactor if WirePlumber monitoring is ever needed outside `AudioService`.
