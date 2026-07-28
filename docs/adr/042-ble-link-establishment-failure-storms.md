# ADR-042 — BLE Link-Establishment Failure Storms and Elapsed-Time Wedge Escalation

**Status:** Accepted
**Date:** 2026-07-26

---

## Context

Following PR #72 (ADR-040), a distinct and unaddressed failure shape persisted:
a passive `connection lost, will reconnect` (the link genuinely dropped, not
the health-check probe from ADR-040) followed by an extended period — minutes
to nearly three hours observed — during which the daemon could not
rediscover/reconnect to the speaker, before succeeding on its own with no
code change or manual intervention. Two occurrences were captured on
2026-07-23/24:

- **21:24:14 → 00:17:38** (~2h53m). 154 scan/connect cycles logged; only 6
  explicit `connection failed (attempt N)` lines, each to a *different* BLE
  address, spaced 9–21 minutes apart — every gap just outside
  `_WEDGE_WINDOW` (600s), so `_note_connect_failure`'s density counter reset
  to 0 almost every time and never reached `_WEDGE_CONNECT_FAILURES` (3). The
  other ~148 cycles were clean-empty scans. `_reclaim_stale_link` (ADR-039)
  ran repeatedly on those empty scans but never found a connected device to
  reclaim.
- **04:32:11 → 04:40:00** (~8m). Same shape, smaller.

Grepping the daemon's own logs across both windows for `health probe failed`,
`UnknownObject`, and `ConfirmedDisconnectError` (the ADR-040 fix's own
signatures) returned zero hits — confirming this is a different failure mode
from the one #72 fixed, not a regression of it.

A `btmon` capture had already been running continuously on the appliance
since 2026-07-22 18:58 (started for unrelated reasons, left running) and
happened to span both windows above in full. This ADR is based on decoding
and analyzing that capture — the first time this specific "extended
unreachable" shape has been examined at the HCI wire level rather than only
through daemon log correlation.

### What the capture ruled out

**Not a rotating-private-address (RPA) scan/connect race.** The leading
hypothesis going into this investigation (recorded in the prior handoff) was
that the speaker's RPA rotates between the scan that discovers it and the
`connect()` that follows, so the manager's `Scanner.find_with_presence()` →
`PartyBoxCandidate.connect()` path (which binds to the *live* `BLEDevice`
handle from discovery specifically to avoid this — see
`packages/partybox/src/partybox/bluetooth/scanner.py`) would still race a
rotation mid-flight. The capture disproves this as the operative mechanism
here: each of the 6 logged failures corresponds to one dense burst of 18–30
`HCI Command: LE Create Connection` attempts over roughly a minute, and
**every attempt within a given burst targets the exact same peer address**
(e.g. `61:DA:CE:67:68:AF` for the whole 21:52 burst, `4E:5F:C6:08:73:7E` for
the whole 22:13 burst, and so on — six different addresses across the six
bursts, matching the six different addresses noted in the original log
correlation, but *stable within* each burst). If the address were going stale
mid-burst from rotation, subsequent attempts in the same burst would target a
new address; they don't.

**Not the orphaned-stale-LE-link condition ADR-039's `_reclaim_stale_link`
targets.** Confirmed independently: the speaker's LE control identity has
never been bonded (`bluetoothctl info <current-LE-address>` shows `Paired:
no`, `Bonded: no`, in contrast to the Classic A2DP address's `Paired: yes`,
`Bonded: yes`), and `_reclaim_stale_link` only ever found nothing during
either window — consistent with there being no leftover `Connected: yes`
BlueZ device object to find. (The lack of LE bonding is real and noted below,
but it isn't what produced these two outages.)

### What the capture found

Every failed attempt shows the same signature:

```
< HCI Command: LE Create Connection (0x08|0x000d)
> HCI Event: LE Connection Complete — Status: Success (0x00)
      Peer address: <addr> (Resolvable)
        ... (link nominally established) ...
> HCI Event: Disconnection Complete
      Handle: 64  Address: <addr> (Resolvable)
      Reason: Connection Failed to be Established (0x3e)
```

The link-layer connection procedure *completes* (`LE Connection Complete:
Status Success`) but then, within roughly a second, fails with HCI status/
reason `0x3E` — the Bluetooth Core spec's "Connection Failed to be
Established / Synchronization Timeout": the central and peripheral fail to
synchronize on an early connection event after the initial handshake. This
repeats every ~1–2 seconds against the same address for the length of the
burst (bleak/BlueZ retrying internally within one `BleakClient.connect()`
call, bounded by `DEFAULT_CONNECT_TIMEOUT`), until the call gives up and
`packages/partybox/src/partybox/bluetooth/bleak_transport.py`'s `connect()`
raises `ConnectionFailedError` — which is exactly the one `"connection
failed (attempt N)"` line the daemon logs per burst. The same signature (30
occurrences) appears in the shorter 04:32–04:40 window. The eventual
successful reconnect in both windows is simply one attempt, in an otherwise
identical retry burst, that doesn't hit 0x3E — i.e. this looks probabilistic
rather than a hard, deterministic block, consistent with a timing/RF margin
issue rather than the speaker or the daemon being definitively stuck.

The appliance's BLE spectrum is demonstrably busy — the same capture logs
thousands of nearby advertisements (Fast Pair–style beacons, etc.) throughout
— which is circumstantially consistent with RF contention affecting the
first-connection-event sync margin, but this capture alone cannot distinguish
that from a Pi-side BCM4345 controller timing quirk (already the suspect
in two other documented issues on this same hardware: ADR-028's UART
corruption and ADR-039's scan-works-but-connect-wedge). Telling those apart
would need either a concurrent RF/spectrum capture or a repeat of this same
capture in a quieter RF environment — out of scope here.

### Why neither existing self-heal mechanism engaged

Both of ADR-039's counters are shaped around *density*:
`_WEDGE_CONNECT_FAILURES` (3 failures within `_WEDGE_WINDOW` = 600s) needs
the six per-burst failures to land close together, but they were 9–21
minutes apart — just outside the window nearly every time, so the counter
kept resetting to 0. `_RECLAIM_EMPTY_SCANS` only fires on scans that come
back clean, and per above, there was never anything to reclaim. Neither
counter is wrong for the failure mode it targets; this is simply a third
shape neither was designed to catch — a long *total* stretch without a
successful connect, made up mostly of clean-empty scans with only sparse,
widely-spaced explicit failures in between.

## Decision

**Add an elapsed-time-based escalation, additive to the existing density
check.** `DeviceManager` now tracks `_unreachable_since` (set on the first
*qualifying* failure of a stretch; cleared on the next successful connect)
and a new `_note_unreachable_duration()` checks it alongside
`_note_connect_failure`'s existing density check. Once
`_WEDGE_UNREACHABLE_TIMEOUT` (600s — equal to `_WEDGE_WINDOW`, not by
necessity but because that value already proved long enough to not trip on
an isolated post-power-command reconnect) elapses without a successful
connect, it requests adapter recovery via the same
`_maybe_recover()`/`_RECOVERY_COOLDOWN` path the density check already uses.

Not every failure qualifies, deliberately — two review rounds on the PR that
shipped this (#88) tightened the original "regardless of how that time was
filled" framing:

- A clean scan that finds no beacon at all — the speaker is off or out of
  range, the ordinary case — does **not** count, and actively clears
  `_unreachable_since`. The first version of this change counted every
  empty scan, which meant a speaker simply left switched off for
  `_WEDGE_UNREACHABLE_TIMEOUT` triggered a real adapter power-cycle, and
  since the clock was never cleared without a successful connect, it
  repeated every `_RECOVERY_COOLDOWN` indefinitely — dropping other live
  connections on the adapter for a state that was never actually wrong.
- Failures inside the ADR-034 post-power-command grace window are exempted,
  same as `_note_connect_failure`. Without this, repeated power-cycling
  (the documented restart flow, or a Home Assistant automation) where each
  reconnect attempt happens to fail inside its own ~15-17s grace window
  could accumulate straight through to an unwanted recovery, even though
  every individual failure is the expected, benign ADR-034 shape.

What *does* count: the speaker's beacon present but its control channel
isn't, an explicit connect failure, or the adapter erroring on scan — outside
any active grace window. Applied to the 2026-07-23 outage (whose empty scans
were beacon-present throughout, per the capture above), this would have
triggered an adapter power-cycle at roughly the ten-minute mark instead of
leaving the speaker unreachable for ~2h53m.

This does not fix the underlying HCI 0x3E link-establishment failures — that
remains either an RF-environment condition or a BCM4345 controller quirk,
neither directly addressable in software — but it ensures the existing
recovery lever (an adapter power-cycle, which *did* eventually let both
observed outages resolve on their own once the sync-failure streak happened
to break) engages reliably on a predictable timescale instead of depending
on the failure happening to be dense enough.

**Instrument `_reclaim_stale_link`'s previously-silent "found nothing"
path**, at DEBUG level, plus a richer diagnostic protocol in companion's
`le_reclaim.py`/`_le_reclaim.py` subprocess helper: the helper now reports
not just how many stale links it disconnected but how many PartyBox-named
LE device objects existed in BlueZ's cache at all (connected or not). During
the 2026-07-23 outage this path ran an estimated 40+ times with zero
visibility into what it actually saw; this closes that gap for future
occurrences without needing a fresh capture every time.

**Not exposed as configuration.** `_WEDGE_UNREACHABLE_TIMEOUT`, like every
other threshold in this file, stays a fixed constant for the same reason
ADR-038 and ADR-040 give theirs: nobody outside this debugging context has a
principled basis to pick a different number yet.

## Alternatives considered

- **Lengthen `_WEDGE_WINDOW` instead of adding a second, orthogonal check.**
  Rejected: `_WEDGE_WINDOW` governs *density between explicit failures*; the
  2026-07-23 outage's problem was mostly clean-empty scans carrying no
  failure signal at all, which a wider density window still wouldn't count.
  A separate elapsed-time check is the more direct fit for "a long stretch
  with mixed failure/empty-scan shape," without changing what the density
  check means for the wedge shape it already correctly handles (ADR-039's
  original 22-failures-in-25-minutes case).
- **Try to fix the HCI 0x3E failures directly** (e.g. connection-parameter
  tuning, retry pacing). Rejected for this ADR: the capture evidence points
  at RF timing margin/controller firmware, neither of which this codebase
  can directly control, and misattributing a fix to the wrong layer without
  being able to reproduce the condition on demand would be guesswork. The
  adapter power-cycle already available via ADR-039 is the correct-altitude
  remedy; the gap being closed here is only that it now reliably triggers.

## Consequences

- A long stretch of clean-empty scans and sparse, widely-spaced connect
  failures — the exact shape the original density-based wedge detection
  couldn't see — now escalates to adapter recovery within
  `_WEDGE_UNREACHABLE_TIMEOUT`, not left to resolve on its own over hours.
- The reclaim path's previously invisible "checked, found nothing" outcome
  is now logged (DEBUG) with an actual count of what BlueZ's cache held,
  making the next occurrence's log analysis possible without a fresh
  wire-level capture.
- **Root cause is only partially closed.** This ADR explains *why* the
  daemon stayed unreachable so long (self-heal design gap, now fixed) and
  *what* was actually failing at the wire level (HCI 0x3E connection
  establishment failures), but not definitively *why* those failures happen
  — RF contention vs. BCM4345 controller quirk remains unresolved. If this
  recurs after this change ships, the escalation should at least bound the
  outage to `_WEDGE_UNREACHABLE_TIMEOUT` + one recovery cycle; whether the
  underlying 0x3E rate itself is affected by anything this project controls
  is an open question for a future investigation with a concurrent
  RF/spectrum capture.
- The speaker's LE control identity remains unbonded (`Paired: no`,
  `Bonded: no`), unlike the Classic A2DP link. This was investigated as a
  candidate explanation (no IRK-based address resolution means BlueZ can't
  correlate a rotated RPA back to an identity) but the capture shows it
  wasn't the operative mechanism in either observed outage — addresses were
  stable within each failure burst. Left as-is; revisit only if a future
  occurrence's evidence implicates it directly.

## References

- ADR-039 (runtime self-heal of a wedged controller — the density-based
  counters this ADR adds an elapsed-time counterpart to)
- ADR-040 (health-check probe tolerance — the prior, distinct fix this
  investigation was confirmed not to be a regression of)
- ADR-015 (BLE GATT control transport, RPA background)
- BLE reconnect investigation handoff, 2026-07-24 (the RPA-race hypothesis
  this ADR's capture evidence supersedes)
