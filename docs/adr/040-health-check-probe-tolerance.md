# ADR-040 — Health-Check Probe Tolerance and Confirmed-vs-Transient Disconnects

**Status:** Accepted
**Date:** 2026-07-22

---

## Context

The Portal intermittently showed "Speaker seems to be on, but not responding"
for anywhere from a few minutes to 20+ minutes — flagged as a v1.0-release
blocker, since it left the speaker uncontrollable remotely for the duration.
Prior investigation this same evening had been chasing this as
Bluetooth-adapter/radio-level flakiness (the recurring BCM4345/controller-wedge
theme covered in ADR-028 and ADR-039), including shipping a manual "Reset
Bluetooth" Portal action as a stopgap.

A `btmon` (HCI-level packet capture) session was left running on the appliance
for over 30 minutes specifically to catch this symptom at the wire level — a
capture this project had never had for this exact failure shape (connect,
"connection lost" logged minutes later, then many minutes of empty scans
before the next connect).

One full cycle was captured cleanly: connect at 19:20:35, "connection lost"
logged at 19:23:23. Decoding the capture around that window showed:

- Every ATT write the daemon sent got a `Write Response` in 60–80ms,
  consistently, right up to the last one at 19:23:05.728.
- Then silence — no further HCI traffic at all — until 19:23:12.474, when the
  capture shows the **local host** issuing the HCI Disconnect
  (`Reason: Remote User Terminated Connection`, i.e. locally initiated).

The link itself was demonstrably healthy for the whole window. What went
quiet was the GATT *application*-layer reply (the notification carrying the
firmware/battery data `DeviceManager` had requested never arrived), and it was
this daemon's own health-check logic — not the radio, not the speaker's
link-layer — that decided ~6.7s of that silence meant "the connection is
dead" and tore it down.

Traced to `DeviceManager._drain_with_health_check()`
(`packages/partyboxd/src/partyboxd/device/manager.py`): a single
`verify_connection()` timeout (or a `ConnectionLostError` propagated from
`_poll_liveness()`'s battery/firmware calls) was converted unconditionally
into a fatal disconnect, with no tolerance — inconsistent with
`_poll_liveness()`'s own established handling of the identical symptom one
layer down (`_LIVENESS_MISS_LIMIT = 2` — a speaker that misses a couple of
liveness probes is marked "asleep", not disconnected).

Redeploying a first version of the fix (a bare retry-once-before-disconnecting
tolerance) surfaced a second, more precise finding within the hour: a
real occurrence logged

```
health probe failed (1/2), retrying next cycle: write to <addr> failed:
[org.freedesktop.DBus.Error.UnknownObject] Method "WriteValue" ... doesn't exist
```

`UnknownObject` is BlueZ confirming the GATT characteristic's D-Bus object no
longer exists — a *confirmed*, not transient, condition. The bare tolerance
gave this one wasted retry cycle before disconnecting anyway. This showed the
first fix's exception handling was too coarse: `ConnectionLostError` was being
used by the transport layer (`BleakTransport`, `packages/partybox`) for two
genuinely different circumstances with no way to tell them apart:

1. A single I/O call failed or timed out — possibly transient, might succeed
   on retry.
2. The disconnect is already confirmed by the platform — bleak's own
   `disconnected_callback` already fired, or (as above) BlueZ reports the
   device/characteristic object itself is gone. Retrying this cannot help.

## Decision

**Health-check tolerance.** `_drain_with_health_check()` retries a failed
cycle up to `_HEALTH_CHECK_FAILURE_LIMIT` (2, matching `_LIVENESS_MISS_LIMIT`)
times before disconnecting, for `ConnectionLostError`/`TimeoutError`. The
counter is a local variable scoped to one call of the method (one
connection's health-checking) rather than instance state, so it can't go
stale across connections or reconnects by construction, independent of call
order in any future refactor.

**`ConfirmedDisconnectError` (new, `partybox.bluetooth.transport`, a subclass
of `ConnectionLostError`).** Raised specifically when a disconnect is already
confirmed — the `_lost` flag from bleak's `disconnected_callback`, or a
`BleakDBusError` whose `.dbus_error` names a known "object already gone"
D-Bus error (`UnknownObject`, `ServiceUnknown`, `org.bluez.Error.NotConnected`)
— as opposed to a plain failed/slow I/O attempt, which stays a plain
`ConnectionLostError`. `_drain_with_health_check()` catches
`ConfirmedDisconnectError` ahead of the generic `ConnectionLostError` branch
and never retries it, same as `NotConnectedError`.

**Not exposed as configuration.** `_HEALTH_CHECK_FAILURE_LIMIT` stays a fixed
constant, not a `SpeakerSettings` field, for the same reason ADR-038 gives for
its own fixed thresholds: nobody outside this debugging context has a
principled basis to pick a different number, and the constant should move if
and when evidence says so (as it did here), not via an operator-facing knob.

## Alternatives considered

- **Increase `_PROBE_TIMEOUT` instead of adding a retry.** Rejected: slows
  every cycle without addressing that one bad cycle was treated as fatal.
- **String-match the D-Bus error message instead of using `BleakDBusError`'s
  structured `.dbus_error`.** Rejected as unnecessarily fragile — bleak
  already exposes the D-Bus error name as a distinct property; matching free
  text would break on wording changes upstream.
- **Reuse `NotConnectedError` for the confirmed-disconnect case** instead of a
  new `ConfirmedDisconnectError` subclass. Rejected: `NotConnectedError`
  already has an established, different meaning elsewhere in this codebase
  (the transport was never connected, or `disconnect()` was called
  explicitly) that `_connect_and_maintain` logs differently
  (`"disconnected from %s"` vs `"connection lost, will reconnect"`).
  Conflating the two would blur that existing distinction.

## Consequences

- A momentarily slow or busy GATT server on the speaker's side no longer
  costs a multi-minute reconnect cycle over one missed probe.
- A confirmed disconnect (the platform already knows the link is gone) is
  still recognized and reconnected immediately — no added latency for the
  case the tolerance isn't meant to cover.
- `ConfirmedDisconnectError` is public SDK surface (`partybox`,
  `partybox.bluetooth`) — a caller that only catches the base
  `ConnectionLostError` is unaffected (inheritance), but a caller that wants
  to distinguish the two circumstances now can.
- **Known gap, not addressed here:** a real disconnect racing with an
  in-flight `verify_connection()`/`_poll_liveness()` call could, depending on
  exactly which error bleak surfaces for that specific timing, be classified
  as transient for one cycle rather than immediately as
  `ConfirmedDisconnectError`. Bounded, not open-ended: the very next loop
  iteration's `drain_until_disconnect()` reads the already-queued disconnect
  sentinel and raises immediately via the passive path (which doesn't go
  through the retry-tolerant branch at all), so the cost is at most one extra
  `_HEALTH_CHECK_INTERVAL`/`_STREAMING_HEALTH_CHECK_INTERVAL` (~15s/~60s), not
  the full retry budget. Not fixed in this PR — would require re-checking the
  transport's internal `_lost` state from outside its own encapsulation, which
  wasn't judged worth the coupling for a narrow, already-bounded race.
- **Not yet confirmed end-to-end.** The original failure is intermittent;
  there had not yet been a second natural occurrence, post-fix, to directly
  observe "would have disconnected under the old code, tolerated under the
  new" at the time this ADR was written.

## References

- ADR-028 (audio readiness model — the earlier, since-corrected BCM4345 UART
  theory)
- ADR-039 (runtime self-heal of a wedged controller — the *automatic*,
  scan/connect-level recovery this ADR's fix is distinct from: that one
  recovers a controller that can't connect at all; this one stops the
  daemon's own health-check from tearing down connections that don't need it)
- PR #72 (`fix/health-check-probe-tolerance`)
