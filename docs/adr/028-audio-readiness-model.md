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

**Problem:** `AudioService` originally ran BlueZ D-Bus calls (`ConnectProfile`, `Device1.Connected`) in the same asyncio event loop as `bleak` (used by `DeviceManager` for BLE GATT — [ADR-015](015-bluetooth-control-transport.md)). `bleak` creates its own `dbus-fast` `MessageBus` in the companion process's event loop. Running a second `MessageBus` in the same loop risks interaction between the two buses. In practice, both `_connect()` and `_is_connected()` returned wrong values: no exception was raised, but no A2DP sink was actually established in PipeWire.

**Decision:** All BlueZ D-Bus calls originating in `AudioService` run in isolated subprocesses via `companion.services._a2dp_connect`. This module is invoked as `python -m companion.services._a2dp_connect <MAC> [connect|check]` and runs its own `asyncio.run()` event loop with a single `MessageBus` — no routing conflict is possible. The subprocess prints `"ok"` / `"err:<msg>"` (connect) or `"true"` / `"false"` (check) to stdout; `AudioService` reads these via `asyncio.create_subprocess_exec()` with a stdout pipe.

`PairingService` ([ADR-027](027-bluetooth-bonding-architecture.md)) is not affected — it creates its `MessageBus` during a distinct operational phase before `bleak`'s bus is active, and manages its own D-Bus connection lifecycle.

**Note on `bluez_dbus.py`:** `BluezClient.connect_a2dp()` / `disconnect_a2dp()` were added during ADR-027 work. These remain in the module but are not called by `AudioService` for the connect/check paths due to the routing conflict; `AudioService` uses the subprocess path exclusively. `_disconnect()` still calls `BluezClient.disconnect_a2dp()` because a teardown path is not timing-critical and the routing issue only manifests under concurrent bleak traffic.

**Why not a shared `MessageBus` or session broker:** The routing conflict is inherent to `dbus-fast`'s asyncio integration and cannot be fixed by configuration. The subprocess approach is the only isolation boundary that is both simple and guaranteed correct.

### `_connect()` contract and post-connect settle

`_connect()` returns `True` when BlueZ accepts `ConnectProfile` — not when the audio transport is available. After BlueZ accepts the request it creates a `MediaTransport1` D-Bus object asynchronously; on real hardware this object typically appears 1–2 s after `ConnectProfile` returns. `_is_connected()` checks for a `MediaTransport1` object on the device path; if run immediately after `_connect()` returns, it finds no transport object yet and returns `False`.

Without a settle delay, this false negative triggers an immediate reconnect attempt. `ConnectProfile` is called again while the first transport is still appearing, causing a rapid connect/disconnect cycle every 2 s — a failure mode observed in early M17.4 validation.

**Decision:** `AudioService` sleeps `_POST_CONNECT_SETTLE = 5.0 s` after a successful `_connect()` before re-entering the top-of-loop `_is_connected()` check. The 5 s value is intentionally conservative: hardware showed the transport reliably appearing within 1–2 s, and 5 s provides margin without meaningful impact on connection latency. The value is the smallest that proved reliable in hardware testing, not a theoretically derived minimum. If it proves problematic on other hardware, the correct replacement is a condition-based wait (see Deferred).

`_set_audio_ready(True)` is called immediately when `_connect()` returns `True` — before the settle sleep — so that the Spotify gate and other consumers are notified without delay.

### MediaTransport1 UUID and A2DP role

`_check()` locates `MediaTransport1` objects under the device path and inspects their `UUID` property. This UUID reflects the **local endpoint's role**, not the remote's:

- When the Pi is the A2DP Source (sending audio to the speaker), `MediaTransport1.UUID` = `0000110a` (A2DP Source UUID)
- `Device1.ConnectProfile` is called with `0000110b` (A2DP Sink UUID) — the remote speaker's role — to initiate the connection

`_check()` accepts both `_A2DP_SOURCE_UUID` and `_A2DP_SINK_UUID` for robustness. Checking only for the Sink UUID caused `_is_connected()` to always return `False` even with an established A2DP link — the other primary cause of rapid cycling in early M17.4 validation.

### `profile-unavailable` as an operator signal

`profile-unavailable` means BlueZ has no registered A2DP handler — WirePlumber's endpoint registration has been lost. This is a local failure, distinct from `br-connection-unknown`, which means endpoints are registered but transport negotiation failed on the speaker side.

A `profile-unavailable` failure is logged as a warning with an explicit recovery instruction: restart the `companion` service. The `ExecStartPre` sequence in `companion.service` always restarts WirePlumber on startup ([ADR-018](018-systemd-service.md)), so a service restart reliably recovers this state.

`_disconnect()` is not called after `br-connection-unknown` errors. Calling disconnect after a failed transport negotiation caused WirePlumber endpoint churn — the next attempt would see `profile-unavailable` instead of `br-connection-unknown`, alternating between the two. Treating `br-connection-unknown` as a plain connect failure (log + backoff retry) avoids this cascade.

### Startup hardening

`companion.service` runs the following `ExecStartPre` sequence before the Python process starts:

```ini
ExecStartPre=+/usr/bin/hciconfig hci0 reset
ExecStartPre=+/bin/chmod 755 /run/user/1000
ExecStartPre=+/bin/sleep 5
ExecStartPre=+/usr/bin/systemctl --user -M pi@ restart wireplumber
ExecStartPre=+/bin/sleep 10
```

The HCI reset ([ADR-023](023-hci-controller-reset-on-startup.md)) clears stale controller state. The `chmod 755 /run/user/1000` ensures the `companion` system user can reach the PipeWire-pulse socket in the `pi` user's XDG runtime directory (`/run/user/1000`), which `systemd-logind` creates with mode 0700 by default — inaccessible to other users. The WirePlumber restart after the HCI reset ensures A2DP media endpoints are freshly registered with BlueZ on every companion start, eliminating degraded endpoint state carried over from a prior session.

The `+` prefix grants these steps transient root elevation without making the main service run as root ([ADR-018](018-systemd-service.md)).

### A2DP connection parameters (M17.4 tuning)

| Parameter | M17.2 value | M17.4 value | Reason for change |
|-----------|-------------|-------------|-------------------|
| `_CHECK_INTERVAL` | 30 s | 60 s | 30 s polling was unnecessarily aggressive for an idle-connected link; the event bus delivers state changes independently of polling, so the interval only affects detection latency for unexpected drops |
| `_AUDIO_GRACE_SECONDS` | 60 s | 300 s | The 60 s window matched the estimated transient-reconnect time but with zero margin; real-world validation showed the speaker sometimes takes longer to accept A2DP after power-on; 300 s gives users 5 minutes before Spotify deregisters |
| `_POST_CONNECT_SETTLE` | — | 5.0 s | New in M17.4; see `_connect()` contract section for rationale |

---

## Consequences

### What changes

- `AudioService.audio_ready` property is the canonical answer to "can we play audio" — consumers use this instead of inspecting BLE state.
- `GET /api/v1/health` returns `ble_connected` and `audio_ready` as distinct fields, enabling external monitors to distinguish the two layers.
- `AudioService.subscribe()` / `unsubscribe()` provide the event bus seam for M17.3 and later consumers.
- `AudioStatus.connected` (existing field, used by `GET /api/v1/audio`) continues to reflect `audio_ready` for backward compatibility with Portal polling.
- All BlueZ D-Bus calls from `AudioService` run in isolated subprocesses (`companion.services._a2dp_connect`) to avoid interaction between `dbus-fast` and `bleak`.
- After a successful `ConnectProfile`, `AudioService` waits `_POST_CONNECT_SETTLE = 5.0 s` before re-checking connectivity, giving `MediaTransport1` time to appear in BlueZ's object manager.
- `companion.service` restarts WirePlumber on every start as part of the `ExecStartPre` sequence.

### What does not change

- BLE GATT reconnection is handled entirely by `DeviceManager` and is independent of `AudioService`.
- The Portal's Bluetooth Audio row already shows A2DP state correctly via the existing `/api/v1/audio` poll and does not require changes.
- `PairingService` uses a direct `MessageBus` (not subprocess) — the `dbus-fast`/`bleak` conflict does not affect it because it operates in a distinct phase before `bleak` is active.

### Hardware validation results (M17.4, 2026-07-02)

| Scenario | Result |
|----------|--------|
| Speaker powered on via `POST /api/v1/power/on` while companion running | A2DP auto-connects; `audio_ready: true` stable; Spotify Connect device appears; music plays end-to-end through PartyBox 520 |
| `audio_ready` stability after UUID fix | No rapid cycling; `_is_connected()` detects A2DP on first check after settle; service enters 60 s `_CHECK_INTERVAL` sleep |
| Speaker power cycle | Grace period preserves Spotify during outage; audio resumes automatically after speaker restores A2DP |
| Pi reboot | A2DP auto-connects; Spotify started without manual intervention |

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
| Sustained `profile-unavailable` (WirePlumber degraded) | `profile-unavailable` is logged with restart guidance; `sudo systemctl restart companion` recovers via `ExecStartPre` WirePlumber restart |

### Deferred

- BlueZ D-Bus `PropertiesChanged` events as an alternative to subprocess polling for `_is_connected()`: would allow near-instant disconnect detection; blocked by the `dbus-fast`/`bleak` routing conflict until a clean shared-bus solution exists or `bleak` is replaced as the BLE transport.
- Portal real-time `audio_ready` updates via WebSocket: the Portal reflects `audio_ready` through the existing `/api/v1/audio` poll. A WebSocket `audio_ready_changed` event would make the Portal fully push-based; deferred to a future milestone.
- Condition-based wait after `ConnectProfile`: `_POST_CONNECT_SETTLE` is a fixed sleep. A more robust replacement would poll `_is_connected()` at a short interval (e.g., 0.5 s) until `MediaTransport1` appears or a timeout (e.g., 10 s) expires — eliminating the guesswork inherent in a fixed sleep. Deferred because the fixed 5 s value proved reliable in hardware testing and the added complexity is not warranted for the current deployment.

### WirePlumber endpoint degradation investigation (2026-07-02)

A dedicated investigation session examined a single continuous boot's `journalctl` output (boot at 06:21, examined through 07:29) that happened to contain both a `profile-unavailable` startup race and a sustained rapid reconnect cycle. Findings reframe the original "~1 hour idle degradation" hypothesis:

**The rapid-cycling failure is not a 1-hour degradation — it starts within minutes of the first successful connection and self-sustains.** In the observed boot, `AudioService` first established A2DP at 06:37:11 (16 min after boot), then immediately entered a connect/disconnect cycle repeating every ~7 s (`ConnectProfile` succeeds → `MediaTransport1` appears → disappears again during the 5 s `_POST_CONNECT_SETTLE` window → `_is_connected()` returns `False` → reconnect). This cycle ran continuously for ~10 minutes across three separate `companion.service` restarts (134 "connection established" log lines in that window) and only stopped after a fourth restart. The deployed code was byte-identical across all four restarts (confirmed via file hash/grep on the Pi) — the instability is not a Python-level bug in the connect/check logic; it is state external to the application.

**Root cause candidate: BCM4345C0 HCI UART transport errors, not WirePlumber endpoint unregistration.** The kernel logged `Bluetooth: Unexpected continuation frame (len 0)` 19 times across the boot — a known BCM4345/H5 three-wire UART framing defect on the Pi 3B+'s onboard Cypress/Broadcom combo chip. These errors cluster tightly with both failure windows: 4 occurrences within 10 s of the profile-unavailable startup race, and 8 occurrences during the 10-minute rapid-cycling window. `hciconfig hci0` reports `errors:0` at both the RX and TX HCI layer — these are transport-layer (H5/UART) framing errors invisible to BlueZ's own counters, consistent with intermittent controller/link corruption during Bluetooth traffic bursts (which A2DP connect churn itself produces, making this plausibly self-reinforcing).

**`AudioService` has no flap detection.** `_connect()` resets `retry_delay` to `_RETRY_BASE` on every successful `ConnectProfile`, regardless of how long the previous connection lasted. During the observed 10-minute flap, each cycle was treated as an independent fresh success, so no backoff ever engaged — the service kept retrying at the fastest possible cadence (bounded only by `_POST_CONNECT_SETTLE`), continuously generating more HCI traffic while the controller was already exhibiting frame errors.

**Evidence for the "WP restart alone is sometimes insufficient" report:** `wireplumber.service` has `BindsTo=pipewire.service` — restarting WirePlumber alone (as `ExecStartPre` does today) never restarts `pipewire.service`, but restarting `pipewire.service` always cascades to `wireplumber.service` (and `filter-chain.service`) via `BindsTo`. The same boot's journal shows a manual, atomic `pipewire.service` + `wireplumber.service` + `filter-chain.service` restart (not from `companion.service`'s `ExecStartPre`, which only ever touches `wireplumber.service`) — consistent with a prior troubleshooting session finding that WP-only restarts were insufficient and manually restarting PipeWire too was required.

**Implication for Q3 (condition-based wait):** A poll for BlueZ's Audio Sink UUID registration would correctly harden the *boot-time* `profile-unavailable` race (endpoints registering asynchronously after WP start) but would **not** address the rapid-cycling failure mode, since in the observed case `ConnectProfile` was succeeding and endpoints were registered throughout — the transport itself was being torn down after connecting, not failing to connect. These are two distinct failure modes that were previously conflated under "WirePlumber endpoint degradation."

**Recommended follow-up (status as of 2026-07-04):**
1. **Implemented.** Flap detection is in `AudioService` (`_FLAP_WINDOW` / `_FLAP_LIMIT` / `_FLAP_COOLDOWN`): a connection surviving less than 20 s counts as a flap, and three consecutive flaps trigger a cooldown instead of an immediate retry — self-limiting the loop rather than resetting the controller.
2. **Implemented.** `ExecStartPre` restarts `pipewire.service` (a superset of restarting `wireplumber.service` via `BindsTo`).
3. **Implemented.** The fixed `sleep 10` was replaced with a condition-based wait (`image/config/wait-for-a2dp-endpoints.sh`, installed to `/usr/local/bin/`, polling BlueZ for the Audio Sink UUID via `busctl`, capped at 15 s with unconditional fallthrough). This hardens the boot-time `profile-unavailable` race specifically; per the implication above, it is not expected to affect the rapid-cycling mode.
4. **Deferred.** Investigate whether a newer `linux-firmware`/kernel on the Pi resolves the `Unexpected continuation frame` BCM4345 UART errors; a known class of issue on Pi 3B+ Bluetooth combo chips independent of this codebase.

> **Superseded root cause:** items 1–2 were mitigations. The flap was ultimately root-caused to WirePlumber's seat-monitoring under PipeWire version skew and fixed by running the `main-embedded` profile — see the "WirePlumber/PipeWire version skew" section below, which is the definitive resolution. Under M18 validation (RC13) the flap did not recur across 9 h idle plus a full day of churn (zero `profile-unavailable`).

### WirePlumber/PipeWire version skew (2026-07-02)

**The appliance was running an unsupported PipeWire/WirePlumber version combination.** That is the central finding of this investigation; everything below — the flap symptom, the diagnosis, and the fix — follows from it. RC12 shipped `wireplumber 0.4.13` (Debian bookworm) running against `pipewire 1.2.7` (Raspberry Pi's own apt repo, which overrides Debian's `pipewire 0.3.65` — already required for camera/HAT support). WirePlumber 0.4.x targets PipeWire 0.3.x internals; this pairing is not one upstream builds or tests against.

A second, distinct flapping failure mode was observed on RC12 (Pi 3B+, PartyBox 520). It produces a superficially similar symptom to the "rapid-cycling" mode described above (`audio_ready` never becomes `true`, `bluetoothd`'s log shows every A2DP endpoint repeatedly registering and unregistering) but has a different signature: endpoints flapped in bursts roughly **once per second** (not once per ~7 s), *all* endpoints churned together under a brand-new D-Bus connection each cycle (not a single `MediaTransport1` disappearing), and it occurred even with `vcgencmd get_throttled` reading `0x0` and zero `Unexpected continuation frame` kernel errors — evidence against the BCM4345 HCI UART corruption this ADR previously implicated for that other mode.

**Investigation findings, from a live `busctl monitor --system org.bluez` capture and `WIREPLUMBER_DEBUG=3`:**

1. Upgrading to `wireplumber 0.5.8` (from `bookworm-backports`, the version meant to pair with PipeWire 1.x) did not by itself stop the flap — it changed the symptom to *no* endpoints ever registering. Reading `/usr/share/wireplumber/scripts/monitors/bluez.lua` surfaced the mechanism: the bluez5 SPA device monitor is only created while `logind` reports the seat state as `"active"` (`startStopMonitor()`, gated by `config.seat_monitoring = Core.test_feature("monitor.bluez.seat-monitoring")`). On this headless appliance, logind's seat state was observed cycling between `"online"` and `"closing"` and never reaching `"active"`. The evidence strongly indicates that under 0.4.13 the equivalent seat-driven logic was what repeatedly created and destroyed the whole bluez5 monitor (and therefore every endpoint) roughly once a second — i.e. that this seat-state gating, not HCI-level corruption, is what drove the flap.
2. `wireplumber.conf` ships a profile for exactly this class of deployment: `main-embedded` ("Typical profile for embedded use cases, systemwide without maintaining state"), which inherits `mixin.systemwide-session` (`support.logind = disabled`, `monitor.bluez.seat-monitoring = disabled`, plus other desktop-only features) and `mixin.stateless`. This is not a workaround bolted onto a desktop configuration — it is the operating mode WirePlumber ships for headless/embedded deployments, and this appliance is one. Running WirePlumber with `-p main-embedded` (via a systemd user-service drop-in) stopped the flap: confirmed stable (zero endpoint churn) over multiple minutes, versus dozens of churn cycles per minute before, and confirmed stable across a full `sudo reboot`.

**A UPower red herring, noted for future investigators:** `journalctl` showed `wireplumber[…]: Failed to get percentage from UPower: org.freedesktop.DBus.Error.NameHasNoOwner` at the same cadence as the flap (the PartyBox 520 exposes a GATT battery service; PipeWire's bluez5 plugin tries to mirror it to UPower's `DisplayDevice`, and `upower.service` was not installed on RC12). Installing and enabling `upower` eliminated those log lines but did **not** stop the flap — it was a coincidental symptom of the same monitor teardown/rebuild cycle, not a contributing cause. Not required for the fix; harmless to have installed.

**Fix (image-level, `image/install.sh`):**
- `wireplumber` is installed from `bookworm-backports`, pinned to `0.5.8-1~bpo12+1` (not Debian bookworm's `0.4.13-1`). As with `UV_VERSION` elsewhere in `install.sh`, the pin is not only about reproducible builds — it is a deliberate choice to validate a specific version against real hardware before adopting it, rather than floating to whatever `bookworm-backports` happens to carry at build time. A future version bump should go through the same hardware validation this one did, not be assumed safe by default.
- `image/config/wireplumber-embedded-profile.conf` is deployed as a `wireplumber.service` (user-session) systemd drop-in, running `wireplumber -p main-embedded` — selecting the correct WirePlumber operating mode for an appliance, not disabling desktop features one-by-one.
- The appliance's Bluetooth rule overrides (AVRCP hw-volume mirroring off, `node.pause-on-idle = false`) were ported from the 0.4.x Lua format (`image/config/wireplumber-appliance.lua`, removed) to 0.5.x's native SPA-JSON `monitor.bluez.rules` format (`image/config/wireplumber-appliance.conf`, layered via `/etc/wireplumber/wireplumber.conf.d/51-appliance.conf`) — 0.5.x does not read the old `~/.config/wireplumber/bluetooth.lua.d/` layout at all, so leaving the old file in place would have silently dropped both overrides. There is no compatibility shim retaining both formats; the appliance has exactly one WirePlumber configuration format at a time.

**Relationship to the "rapid-cycling" investigation above:** these are believed to be two independent failure modes that both manifest as endpoint/connection flapping. The BCM4345 HCI UART corruption investigated above remains a plausible cause of *its* symptom (a single `MediaTransport1` disappearing on a ~7 s cycle, correlated with `Unexpected continuation frame` kernel errors) and is unaffected by this fix. The flap-detection and `pipewire.service`-restart recommendations above still apply as defense-in-depth for that separate mode.

**Validated:**
- Endpoint registration stability: exactly one registration at boot plus the one expected re-registration from `companion.service`'s own `ExecStartPre` WirePlumber restart, across a full reboot — no ongoing churn.
- End-to-end: `POST /api/v1/power/on` → A2DP connects → `audio_ready: true` → Spotify Connect device appears (`GET /api/v1/spotify` → `{"running": true, "device_name": "PartyBox Companion"}`).
- The ported `monitor.bluez.rules` take effect on the live connected node, not just parse without error — confirmed via `pw-cli info` against the running appliance: node 76 (`bluez_output.50_1B_6A_14_FD_1D.1`) shows `node.pause-on-idle = "false"` and `node.volume = "1.0"`; device 75 (`bluez_card.50_1B_6A_14_FD_1D`) shows `bluez5.hw-volume = "[]"`.

### Volume floor from mixin.stateless (2026-07-18)

`mixin.stateless` (inherited by `main-embedded`) disables `hooks.device.routes.state`, meaning WirePlumber never saves or restores per-device route volumes. On every A2DP connect, WirePlumber's `apply-routes.lua` sets `channelVolumes` to `Settings.get_float("device.routes.default-sink-volume")` — WirePlumber 0.5.x's compiled-in default is **0.064 (linear)**, which displays as **0.40** in `wpctl`'s cubic-perceptual scale (`0.064^(1/3) = 0.40`). This happened on every boot, capping software volume at 40% regardless of the `node.volume = 1.0` hint in `monitor.bluez.rules`.

The `node.volume` property set by the rule is applied at node creation, but `apply-routes.lua` runs after node creation and wins. The original RC13 validation checked `pw-cli info` for `node.volume = "1.0"` — which was correctly set — but did not verify the actual channel volume via `wpctl get-volume`, so the 40% floor was not caught at that time.

**Fix:** `image/config/wireplumber-appliance.conf` (deployed to `/etc/wireplumber/wireplumber.conf.d/51-appliance.conf`) now includes a `wireplumber.settings` block alongside the existing `monitor.bluez.rules`:

```
wireplumber.settings = {
  device.routes.default-sink-volume = 1.0
}
```

`1.0` linear = 100% perceived volume (wpctl shows `1.00`), the maximum without soft-amplification boost. The override is applied globally (not scoped per-device), which is correct for this single-output appliance.

**Validated (Pi 5, WirePlumber 0.5.8):** `wpctl get-volume @DEFAULT_AUDIO_SINK@` returns `Volume: 1.00` immediately after A2DP connect on a clean boot, with no manual `wpctl` intervention. Confirmed stable across two reboots.

**Resolved (not a new bug):** with the flap fixed, live testing initially surfaced `br-connection-unknown` from `ConnectProfile` after a service-level restart. A full `sudo reboot` was tried next (the tmpfiles.d WirePlumber-state clear from `install.sh` step 10c(5) only fires on an actual reboot, not `systemctl restart`) and `br-connection-unknown` persisted — but the speaker was also physically powered off at that point. `POST /api/v1/power/on` immediately resolved it: A2DP connected on the next retry, `audio_ready` reached `true`, and the Spotify gate started `librespot`. `br-connection-unknown` in this instance was BlueZ correctly reporting "no BR/EDR link" because there was no speaker to link to — not a WirePlumber or endpoint-registration issue.

### Deferred: runtime self-healing needs a privileged broker, not a runtime sudo grant

The flap-detection cooldown (follow-up item 1 above) only slows down retries — it cannot perform the actual fix (`hciconfig hci0 reset` + `pipewire.service` restart), because `AudioService` runs unprivileged as `companion` and `NoNewPrivileges=true` blocks any sudo escalation from within the running process. (This is why the earlier `_maybe_restart_wireplumber()` attempt, which tried exactly this, was dead code and was removed.) Today, a controller that wedges mid-session only fully recovers via `sudo systemctl restart companion`, i.e. by re-entering the startup path from outside.

The naive next step — grant `companion` a new sudoers rule for `hciconfig hci0 reset` and/or `systemctl --user -M pi@ restart pipewire`, mirroring the now-removed `companion-wireplumber` grant — was considered and rejected. Expanding `companion`'s privileges piecemeal every time a new runtime recovery need appears works against the least-privilege design in [ADR-019](019-distribution-approach.md): it makes the unprivileged appliance process a growing root-adjacent surface, one grant at a time, with no place that reasons about *whether* a given recovery attempt is safe (rate-limited, audited, consistent with concurrent operations).

The considered alternative: a small, narrowly-scoped **privileged recovery broker** — a separate root-owned component (or a tightly constrained systemd unit invoked via socket activation, a D-Bus method, or similar) that `AudioService` *requests* recovery from, rather than performing privileged operations itself. `AudioService` would signal "I believe the controller is wedged" through a narrow, auditable channel; the broker — not the appliance process — decides whether and how to act, and is the only component whose privilege footprint needs to grow. This keeps today's privilege model intentionally simple (no new grants) while leaving a clear path to runtime self-healing if it's ever prioritized.

This is recorded as a **possible future direction, not a plan** — whether runtime self-healing is worth building (vs. accepting that a wedged controller requires an operator or the existing `Restart=on-failure` / manual restart path) is a product decision, not a missing implementation. No code changes follow from this note.
