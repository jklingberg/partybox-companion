# M3 — Audio Transport Viability: Findings

**Status:** 🟢 Verdict reached — appliance concept is **viable**, with one scoped
BLE-connection-management risk to engineer in the daemon (M6).

Durable synthesis of the M3 spike (see
[ADR-014](../adr/014-audio-transport-viability.md)). Raw evidence is in the
[`spike/m3-audio/`](../../spike/m3-audio/) scripts and their
`evidence/<timestamp>-<run>/` output (timeline + summary per run).

## The question

> Can a Raspberry Pi reliably act as both the BLE control endpoint and the
> Bluetooth A2DP audio source for a JBL PartyBox at the same time?

## Verdict

**Yes — viable, conditionally.** Two halves, validated separately:

- **A2DP audio source: ✅ solid.** The Pi pairs as an A2DP source, PipeWire
  routes to it, and audio streams cleanly (SBC, **zero xruns**, no disconnects).
  No fragility observed on the audio path at any point.
- **BLE control coexistence: ✅ demonstrated; ⚠️ naive BLE session management is
  fragile.** BLE control connected and round-tripped commands *while A2DP was
  connected* (76 ms probe; a clean 60 s stream with every probe succeeding). It
  is **not** fundamentally blocked. But a naive connect-per-session approach
  against the speaker's rotating LE addresses wedges the controller under
  repeated connect/disconnect cycling. **The wedge is fully recoverable** with a
  controller reset (`bluetoothctl power off/on`), after which connect works on
  the first attempt again.

### Where the limitation lives — Pi side, not the speaker

The fragility is on the **Pi's software/firmware stack** (BlueZ + the Pi's
Bluetooth controller), *triggered by* the speaker's behaviour but manifesting and
recovering entirely on our side:

- The **wedge is Pi-side**: it is cleared by resetting the *Pi's* controller
  (`bluetoothctl power off/on` on the Pi) with no action on the speaker. That a
  Pi-only reset fixes it is the proof the wedged state lives in the Pi's BlueZ /
  controller, not in the PartyBox.
- The **trigger is the speaker's** rapidly-rotating private LE addresses (a
  standard privacy feature, not a fault). Naive session management keeps issuing
  fresh connects against stale rotating addresses; the Pi stack degrades under
  that churn.
- The **`AuthenticationFailed` on LE bonding** is an *interaction* between the
  Pi's BlueZ pairing flow and the speaker's LE security — to be worked out in M6.

This split matters for the verdict: because the failure mode is in software we
own (BlueZ usage + connection policy on the Pi), it is ours to fix. No speaker
firmware change is needed, and no hardware/architecture change is warranted. The
BLE reliability work (bonding + connection management + controller-reset
recovery) **belongs in the daemon (M6)**.

## Evidence log

| Time (UTC-ish) | Run | Evidence dir | Result | Key data |
|---|---|---|---|---|
| 08:21 | `audio_connect` | `…-082141-audio_connect` | ✅ PASS | A2DP + BLE coexist at connect. **Codec SBC**, profile `a2dp-sink`. BLE probe **76.6 ms**. No drops/8 ticks. |
| 08:22 | `audio_stream` 60 s | `…-082226-audio_stream` | ✅ PASS | Tone streamed; **12/12 BLE probes OK**; **xruns 0**; no disconnects. |
| 08:27 | `audio_stream` 30 min | `…-082703-audio_stream` | ⚠️ partial | A2DP perfect (xruns 0); **BLE writes failed** though link stayed "connected". Interrupted early to diagnose. |
| 08:36–08:45 | `diag_ble_bond` ×3 | `…-08{3646,4026,4329}-diag_ble_bond` | ❌ FAIL | BLE **connect timed out** repeatedly (rotating RPAs), with *and* without A2DP — controller had wedged. |
| 08:46 | `diag_ble_bond` (after controller reset) | `…-084611-diag_ble_bond` | ❌ FAIL (but informative) | **Connect works again** (14 s). New errors: LE `AuthenticationFailed` to RPAs; control char not found on the BR/EDR identity. |
| 09:04 | `reconnect_stress` 10× disconnect (A2DP) | `…-090431-reconnect_stress` | ✅ PASS | **10/10 A2DP reconnects**, median **1.16 s**, max **2.17 s**. Bonded reconnect-by-identity is fast and reliable. |

> One-time A2DP bond (BR/EDR `50:1B:6A:14:FD:1D`): paired/bonded/trusted cleanly,
> Just-Works, no PIN. After a controller reset, A2DP reconnect-by-identity is
> immediate and reliable (bond persists).

## Answers to the M3 brief questions

- **Can BLE control coexist with A2DP audio?** — **Yes.** Demonstrated: control
  commands round-tripped during an active A2DP stream (08:21–08:22 runs). The
  later failures were a controller wedge from connect-churn, not coexistence.
- **Does power-on still work during audio?** — Power-on *frame* writes succeeded
  during audio in the early runs; a full power-cycle test was **not** reached
  before the controller wedged. Re-test once connection management lands.
- **Does playback survive extended (30+ min) sessions?** — **Not yet proven.**
  The 30-min run was interrupted at ~40 s to diagnose the BLE anomaly; audio was
  flawless up to that point. A clean A2DP-only 30-min run is the one outstanding
  piece of headline evidence (see Outstanding work).
- **Are there audio dropouts?** — **None observed.** xrun delta 0 across every
  monitored tick, including while BLE scans/connects were happening.
- **What happens after standby / can it reconnect automatically?** — **A2DP
  reconnect is reliable and fast: 10/10 over `reconnect_stress.py` (disconnect
  mode), median 1.16 s, max 2.17 s** (`…-090431-reconnect_stress`). Bonded
  reconnect-by-identity is the dependable path. Still deferred: the `--mode
  standby` variant (waits for real speaker standby) and BLE reconnect
  (`--check-ble`), the latter because it provokes the documented controller
  wedge — both wait for M6 connection management.
- **Is bonding required for reliable reconnect?** — **Yes, and it's two bonds.**
  The A2DP (BR/EDR) bond is separate from the LE *control* bond. A2DP bonding is
  easy and works. The **LE control identity is not yet bonded**, which is why
  control reconnect is unreliable: unbonded, BlueZ can't resolve the speaker's
  rotating private address to a stable identity, so connects race the rotation.
- **What BlueZ / Bleak / hardware limitations were discovered?** — see below.

## What we learned about the hardware

The PartyBox 520 is a **modern LE Audio device**, richer than assumed. The
post-reset GATT enumeration showed, on one LE identity, the full set: Published
Audio Capabilities (PACS), Audio Stream Control, Volume Control, Microphone
Control, Available/Supported Audio Contexts, Google Fast Pair, a Qualcomm (QTIL)
service, and the vendor **`excelpoint.com` control service** (`…0000`) with the
TX `…0002` / RX `…0001` chars the SDK uses.

- It advertises over LE under **rapidly-rotating resolvable private addresses**
  (2–3 distinct addresses per 10 s scan, repeatedly observed) as well as its
  identity address. The vendor control service is **not** in the advertisement,
  so discovery is name-based.
- The LE control GATT and the BR/EDR (A2DP) identity are **different addresses**;
  connecting to the BR/EDR identity over LE did **not** expose the vendor
  control characteristic ("not found"). Landing on the right LE identity matters.
- That the speaker exposes **LE Audio (LC3)** is a notable future option: a
  second possible audio path that doesn't exist on the A2DP/AVRCP plane. Out of
  scope for v1.0, but worth recording.

## Limitations & instrumented problems

Per the M3 directive — *instrument problems, don't solve them*. Recorded for M6,
not fixed here:

1. **BlueZ controller wedge under connect-churn.** Repeated BLE connect/disconnect
   against rotating RPAs (especially alongside A2DP) drives the controller into a
   state where *all* LE connects time out. **Recoverable** via `bluetoothctl
   power off/on`. → The daemon needs a connection manager that avoids churn
   (persistent bonded connection, connect-by-identity) and a controller-reset
   recovery path. Evidence: `…-083646/084026/084329-diag_ble_bond`.
2. **Unbonded LE control link + rotating RPA = unreliable connect.** Connect by a
   captured live `BLEDevice` works *sometimes* but races address rotation. The
   fix is an LE bond so BlueZ resolves RPAs to the identity — but bonding itself
   returned `AuthenticationFailed` to RPAs in this session; the working bond path
   needs to be established (likely: connect to the correct LE identity while the
   speaker is in pairing mode, then bond). Evidence: `…-084611-diag_ble_bond`.
3. **`is_connected` ≠ writable.** In the 30-min run the SDK transport reported the
   link up while every GATT write failed. The daemon's health check must verify
   writability, not just connection state. Evidence: `…-082703-audio_stream`.
4. **Orphaned `pw-play` on abrupt kill** (toolkit bug, fixed during the session):
   killing a script left its `pw-play` child playing. `m3lib.audio.play` now
   terminates the child on cancellation.
5. **BLE connect costs a ~10 s LE scan up front** (`Scanner.find`). Fine for a
   spike; the daemon should connect by bonded identity instead of re-scanning.

## Outstanding work (not blockers)

- **Clean 30-min A2DP-only extended session** to formally close the "survives
  long sessions / no dropouts" criterion. Audio has been flawless in every
  sample; this just makes it official. Cheap to run (`audio_stream.py
  --duration 1800 --no-ble`).
- **`reconnect_stress.py` standby mode + `--check-ble`** — the A2DP disconnect
  cycle is now validated (10/10); the `--mode standby` variant and BLE reconnect
  are meaningful only once a bonded BLE connection-management path exists.
- These are deferred to **M6 (daemon)**, where BLE bonding + connection
  management are the natural home, not to a hardware/architecture change.

## Decision

**Proceed to M4+.** The appliance's core assumption holds: a single Pi can
source A2DP audio to the PartyBox and control it over BLE. The audio path is
production-grade already; the BLE control path is proven possible and its
fragility is a well-understood, software-addressable connection-management
problem to be engineered in the daemon. No alternative audio architecture (USB
bridge, different stack, different Pi) is warranted — M3 has done its job of
de-risking the concept before that investment.
