# ADR-044 — Radio Contention: Audio Has Priority Over the Control Link

**Status:** Accepted
**Date:** 2026-08-11
**Amends:** [ADR-039](039-ble-controller-wedge-self-heal.md) (adapter recovery is now gated on playback), [ADR-042](042-ble-link-establishment-failure-storms.md) (reframes what a "storm" costs)
**Builds on:** [ADR-028](028-audio-readiness-model.md) (the scan-cost finding), [ADR-035](035-state-ownership-and-signal-pipeline.md)
**Depends on:** PR #107, which introduces `AudioService.radio_busy()` and wires it in as `DeviceManager`'s `streaming_fn`. This ADR's gates consume that signal, so they cover the BR/EDR reconnect page as well as steady-state streaming — a scan is equally unwelcome during either.

---

## Context

The v1.0 UX review (validation run
[`2026-08-11-v1.0-physical-ux`](../validation/runs/2026-08-11-v1.0-physical-ux.md),
scenario PHYS-12) set out to explain the two failure reports that dominate
real use: *"PartyBox doesn't show up in Spotify"* and *"the music stutters"*.
They turned out to be the same bug, and the appliance was inflicting both on
itself.

### What was observed

Starting playback reliably knocks out the BLE control link about a minute
later:

```
19:53:11  A2DP connection established
19:54:12  connection lost, will reconnect     ← 60.3s after audio started
19:54:12  scan attempt 2
19:54:25  scan attempt 3
19:54:43  scan attempt 4
19:55:11  scan attempt 5
19:55:59  scan attempt 6
19:56:16  connected (attempt 6)
```

No `health probe failed` line precedes the loss, so this is a
platform-signalled disconnect (bleak's disconnected callback), not an
ADR-040 probe false-positive. The operator, listening live, reported "the
standard 30s after start stuttering" — several audible seconds beginning
shortly after playback and thinning out as the backoff widened. "Standard"
is the important word: this is what every listening session does.

The stutter is not a mystery. ADR-028 established by btmon capture that on
this BCM4345 combo controller an LE scan's **start/stop mode switch** costs a
~440 ms cluster of A2DP TX gaps, independent of scan duration, and that no
interval short of "never" avoids it. `AudioFocusService` was consequently
changed to skip its scans outright while audio flows. `DeviceManager`'s
reconnect loop was never given the same treatment: `_connect_and_maintain`
called `_scan()` unconditionally, and the `streaming_fn` gate added later
relaxed only the *health-check* interval. So the loop that runs precisely
when the link is down — which, per the above, is precisely when music is
playing — scanned freely into the stream.

### The expensive tail

On 2026-08-09 the same start-of-playback drop occurred, but the reconnect
did not land in two minutes:

```
16:40:34  connection lost (43s after connect, audio playing)
          … 25 LE scans over 11 minutes, music playing throughout …
16:51:34  requesting Bluetooth adapter recovery: 652s without a successful
          connect (regardless of failure density) (ADR-039)
16:51:35  adapter reset: hci0 power-cycled
16:51:45  A2DP sink not connected, connecting to 50:1B:6A:14:FD:1D
16:51:45  audio gate: audio unavailable — 300s grace before stopping Spotify
16:51:46  A2DP connect failed: err:'br-connection-unknown'   (×5)
16:54:12  5 consecutive outright connect failures — cooling down 300s
16:56:45  audio gate: grace period expired — stopping Spotify Connect
17:06:59  connected (attempt 244)
```

ADR-039's watchdog fired and power-cycled `hci0`. That reset destroyed the
A2DP link as collateral damage — `AudioService` noticed exactly ten seconds
later — and the speaker then refused reconnection with
`br-connection-unknown` for long enough to burn the five connect retries, the
300 s connect cool-down *and* the 300 s Spotify grace. librespot was killed,
the Zeroconf advertisement withdrawn, and the appliance disappeared from every
Spotify client for 26 minutes.

`_maybe_recover`'s own docstring had said all along that a recovery
power-cycle "drops every connection on the adapter (including healthy A2DP
audio)". The hazard was understood and written down; it simply was not gated.

### The mistake in the existing design

Both defects share one premise: that a missing BLE control link is a fault,
and that the appliance should spend radio time and, eventually, an adapter
reset getting it back.

On this hardware that premise is wrong while audio is playing. The speaker
appears to drop the LE link when a Classic audio stream starts, and the two
subsystems contend for one radio. The appliance was interrupting the user's
music to chase a link the hardware had decided not to hold — and the chase
does not even work faster for the effort. It only makes the failure audible.

---

## Decision

**While A2DP audio is streaming, audio wins every contention for the radio.**

Concretely, in `DeviceManager`:

1. **The scan/connect loop is deferred entirely while streaming.**
   `run()` consults `_defer_for_streaming()` before each cycle; when audio is
   flowing it sleeps `_STREAMING_SCAN_RECHECK` (10 s) and skips. No LE
   discovery, no connect attempt, nothing on the radio at all. This is the
   same remedy, for the same reason, on the same evidence, that
   `AudioFocusService` already applies to its own scans.

2. **Automatic adapter recovery never runs while streaming.**
   `_maybe_recover()` defers, preserving its counters and setting no
   cool-down, so it fires on the first qualifying failure after playback
   stops. The Portal's manual reset is *not* gated: an operator asking for it
   explicitly has overridden this judgement.

3. **Deferred time does not accrue toward the wedge timeout.**
   On release, `_defer_for_streaming()` shifts `_unreachable_since` forward by
   the duration just spent deferred. Otherwise a long listening session would
   hand ADR-039's watchdog a 600 s+ "unreachable" measurement the instant the
   music stopped, and trigger a power-cycle for a link that had not yet been
   given one chance to reconnect.

   The window is **frozen, not reset**. Simply clearing `_unreachable_since`
   would be wrong once `streaming_fn` is `radio_busy` (below): that reports
   True during BR/EDR connect pages as well as during playback, and those
   recur precisely when things are going badly. Every failed A2DP retry would
   restart the wedge timer, and ADR-039 could never fire in the scenario it
   exists for. Freezing keeps a genuine wedge accumulating across playback,
   just without counting the time we deliberately stood down.

4. **`streaming_fn` failures collapse to "not streaming".**
   `_is_streaming()` centralises the call and swallows exceptions, yielding
   pre-ADR-044 behaviour on a failing probe rather than silently disabling
   reconnection forever. `__init__` already documented *streaming_fn* as never
   raising; this makes that true at the call site rather than trusting it. An
   exception escaping the old call site would have killed the manager task.

### What this accepts

**BLE control being down while audio streams is now a normal, expected steady
state** — not a transient to be recovered from. For as long as music plays:

- `ble_connected` reports false and the Portal shows the control link down.
- Power, battery and firmware commands are unavailable.
- `speaker_state` freezes at its last reading.

This is a real functional loss and it is the price of the decision. It is
accepted because playing music without artefacts is the appliance's primary
job; because the control link is not reachable during playback anyway, so the
loss is descriptive rather than caused by us; and because the user retains
volume, play/pause and skip through Spotify Connect and AVRCP, which do not
depend on the vendor GATT link at all. Power-off is the one genuinely useful
command that becomes unavailable mid-playback — the user can stop playback
first, and it returns within ~10 s.

---

## Alternatives considered

**Coordinate scanning with a mutex instead of deferring.** Rejected for the
reason ADR-028 gives for the identical choice in `AudioFocusService`: the cost
is the mode switch itself, so serialising scans against other scans does not
reduce the number of switches landing in the stream. It also adds
cross-service coordination to a resource-constrained Pi for no benefit.

**Keep scanning but at a much longer interval.** Rejected on the same evidence
that killed it for FDDF: relaxing cadence (12 s → 3 s window, 60 → 120 → 300 s
interval) reduced click *frequency* and never per-click *severity*, and the
user judged clicks every few minutes unacceptable. There is no interval short
of "never" that works.

**Fix the root cause — stop the speaker dropping LE when audio starts.**
Out of reach: it is the speaker's firmware behaviour, not ours. Worth
revisiting if a connection-parameter change proves to hold the link through
stream start, but it cannot gate v1.0.

**Gate only the adapter recovery, leave scanning alone.** Rejected: it fixes
the 26-minute outage but leaves the everyday stutter, which is the symptom
users actually report. The two gates address the two tails of one event and
are worth little separately.

---

## Consequences

- The stutter at the start of every listening session is removed at source.
- The self-inflicted 26-minute Spotify outage cannot recur: the only
  mechanism that broke a healthy A2DP link is no longer permitted to run
  while one exists.
- ADR-039's watchdog remains fully in force whenever audio is *not* playing,
  which is when a genuinely wedged adapter actually needs recovering and when
  a reset is free.
- ADR-042's failure storms become far cheaper. A multi-hour unreachable
  window with music playing now costs nothing on the radio instead of ~150
  audible scans.
- The Portal must not present `ble_connected: false` during playback as an
  error state. Tracked as follow-up; the honest rendering is closer to
  "controls unavailable while playing" than to "disconnected".
