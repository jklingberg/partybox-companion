# 09 — Hardware Safety & Resource Efficiency Review

A follow-up review, done at the M19-adjacent point where `main` had accrued
14 commits past this register's original base (`#54` idle-battery-shutdown,
2026-07-11) — including audio-focus detection, the WirePlumber volume fix,
real Spotify playback state, and the manual Bluetooth reset action. Framed
around a specific brief: could anything here stress the PartyBox hardware,
and does the software hold up if the stated target hardware (Pi Zero 2 W —
see [ARCH-01](01-architecture-review.md#arch-01)) becomes the primary board
rather than an aspiration? Every source file in `packages/`, the Portal JS,
`system/systemd/companion.service`, and `image/install.sh` were read for
this pass.

Several findings here are **new evidence for existing findings**, not new
problems — cross-referenced rather than re-argued. Genuinely new findings
get new `PERF-*` IDs, per this register's ID rules (see
[README.md](README.md)).

## One-paragraph verdict

**No finding in this pass rises to P0/hardware-damage.** The write surface to
the speaker is three opcodes (power, firmware request, battery request — see
`partybox/protocol/constants.py`), all ATT write-with-response (flow
controlled), with no firmware-update path, no raw/fuzzable payload API, and
volume writes short-circuited by `NotImplementedError` before they ever reach
the transport. Power cycling is human-triggered only and self-limits to
roughly one cycle per ~40s (the speaker's own ADR-034 BLE-stack reset). The
real cost of this codebase's current shape is **resource waste, not hardware
risk**: a subprocess-per-D-Bus-call pattern (already ARCH-01) that spawns
several Python interpreters per minute, and a fixed 15 s dual-probe liveness
cadence that runs identically whether the speaker is dancing or asleep at
3 a.m. Both are exactly the kind of cost that is invisible on a Pi 5/USB-mains
budget and decisive on a Pi Zero 2 W / battery-fed budget.

---

## Part A — What's already right (resource/safety lens)

Endorsements specific to this pass's brief, checked against source:

- **Every retry loop in the Bluetooth path has a cap, cooldown, or gate** —
  DeviceManager's wedge detection (dense-failure windows, ADR-034 power-command
  grace, 900 s recovery cooldown, separate manual debounce —
  `partyboxd/device/manager.py:84-140`), AudioService's flap/failure cooldowns
  (`companion/services/audio.py:57-77`), and the `retry_now`/`recheck_now`
  signal split that closed a documented reconnect→hammer→drop loop. Nothing
  found can run away and hammer the radio or the speaker indefinitely.
- **The standby gate is a genuine hardware-safety feature, already shipped** —
  `AudioService.run` (`audio.py:352-371`) stops BR/EDR paging a speaker known
  to be asleep, built directly from the 2026-07-18 finding that 148/151
  "Unexpected continuation frame" kernel errors sat within 15 s of an
  outgoing page attempt. This is the one place this review would point to as
  evidence the team already thinks in these terms — it just doesn't yet cover
  every state that deserves it (see PERF-02).
- **No unsolicited AVRCP, no SDP polling, no gratuitous connects anywhere.**
  Power commands are the only writes the appliance sends without being asked.
- **Scan coordination across independent scanners is real**: AudioFocus scans
  are skipped outright while DeviceManager holds no BLE connection
  (`audio_focus.py:193-198`), added specifically after a live-caught ADR-039
  wedge was correlated with overlapping scans. The two scanners cannot run
  concurrently by construction.

---

## Part B — New findings

### PERF-01 — Fixed 15 s dual-probe liveness cadence runs identically awake or asleep
**Severity:** P2 · **Status:** OPEN · **Where:** `partyboxd/device/manager.py:45` (`_HEALTH_CHECK_INTERVAL`), `_drain_with_health_check` (573-615), `_poll_liveness` (617-693)

Extends **[ARCH-02](01-architecture-review.md#arch-02)** and the roadmap's
v1.3 item ("makes standby detection push-based — delete the 15 s probe
loop") with quantified cost, and proposes a smaller interim step that does
not require the full demux rewrite.

Every 15 s, connected or not: `verify_connection()` writes a
`FirmwareVersionRequest`, then `_poll_liveness()` writes a
`BatteryStatusRequest` and — if that times out (3 s) — a second
`FirmwareVersionRequest` fallback (another 3 s timeout). An **awake** speaker
takes 2 writes/cycle (~11,500/day); a speaker in **standby** takes 3
writes/cycle *and* burns 6 s of timeout waiting, all night, unconditionally.
The drain task is also cancelled and recreated every cycle
(`asyncio.create_task(device.drain_until_disconnect())`, line 586) — churn
that is currently structural (single-consumer receive queue, ARCH-02) but
adds up over months of uptime.

None of this can hurt the speaker (§ verdict), but it's continuous BLE radio
duty on both ends and a plausible reason a "standby" speaker never reaches
its deepest sleep state, and it's 2-3x more Pi wakeups than the state
actually requires.

**Recommendation (interim, before the ARCH-02/v1.3 demux):** (1) drop the
separate `verify_connection()` write — a `BatteryStatusRequest` already
round-trips ATT, so a reply proves link+awake and a clean timeout proves
link-alive-but-asleep; this alone cuts 2-3 writes/cycle to 1. (2) Once
`speaker_awake` is `False`, stretch the interval (e.g. 60 s), mirroring the
`_STANDBY_RECHECK` philosophy AudioService already uses
(`audio.py:91`). Wake latency cost is low: the speaker typically announces
waking by paging the Pi for A2DP regardless of this loop's cadence.

### PERF-02 — A2DP retry ladder pages a beacon-less "off" speaker forever
**Severity:** P2 · **Status:** OPEN · **Where:** `companion/services/audio.py:352-371`

The standby gate (Part A) only fires for `speaker_state == "standby"`. For
`"off"` (BLE disconnected, **no** FDDF beacon seen — the speaker is
unpowered, not merely asleep — see `StatusSnapshot.beacon_seen`,
`partyboxd/device/manager.py:178-200`), AudioService keeps walking the full
retry ladder (5 attempts, 300 s cooldown, repeat) indefinitely. A page train
to an unpowered speaker cannot succeed by definition — the same
"Unexpected continuation frame" cost the standby gate was built to avoid
recurs, just against a speaker that's unplugged rather than asleep, for as
long as it stays unplugged.

**Recommendation:** extend the existing gate condition to
`speaker_state in ("standby", "off")` with the same slow safety-net recheck
shape already used for standby (`_STANDBY_RECHECK`). `beacon_seen` already
makes `"unreachable"` (powered, page might work) distinguishable from `"off"`
(unpowered, page cannot work) — this is a small conditional change, not new
plumbing.

### PERF-03 — AudioFocusService's cost is high relative to its value, and it already ships disabled
**Severity:** P2 · **Status:** OPEN · **Where:** `companion/services/audio_focus.py`, `companion/services/_fddf_scan.py`

Two subprocess spawns per minute (`transport_active()` state check +
`_fddf_scan` window) plus a continuous LE discovery window every 60-120 s,
to power a single advisory "someone else is connected" banner. Per the
project's own incident backlog, `_fddf_scan.py` currently ships `.disabled`
on the real Pi — i.e. **production is already running without this feature**,
which is itself evidence its absence is tolerable, and a warning that
re-enabling it in its current form re-introduces real steady-state cost for
a UI hint.

**Recommendation, cheapest first:** (a) scan only while a Portal client is
actually connected (no viewer, no value from a UI-only signal); (b) scan
only when the suspicious state is plausible (transport `active` **and**
Spotify `playing` simultaneously, rather than a fixed cadence always); (c)
fold it into the ARCH-01 broker (see PERF-04) as a passive listener riding
along on discovery windows that already exist for other reasons, eliminating
its own subprocess entirely.

### PERF-04 — `GetManagedObjects` polling instead of `PropertiesChanged` push (adds detail to ARCH-01)
**Severity:** P2 · **Status:** OPEN · **Where:** `companion/services/audio.py:475-526`, `companion/services/_fddf_scan.py:112-114`

**[ARCH-01](01-architecture-review.md#arch-01)** already calls for a single
broker subprocess and names the `PropertiesChanged` subscription it would
unlock (deleting the 60 s A2DP poll) as a direct benefit. Adding the
specifics found in this pass: the A2DP `check`/`state` subprocess calls
serialize **bluetoothd's entire cached object tree** to answer one boolean —
in a dense RF environment (apartment building, event venue) that is every
device bluetoothd has ever cached, not just the paired speaker.
`_fddf_scan.py` independently introspects every cached `Device1` object per
scan window (lines 112-114) rather than using
`SetDiscoveryFilter(UUIDs=[FDDF])` to have bluetoothd filter server-side.
Both are free wins once ARCH-01's broker exists, and the filter change is
free even before it.

### PERF-05 — `Supervisor.last_exception` retains a full traceback (and its locals) indefinitely
**Severity:** P3 · **Status:** OPEN · **Where:** `companion/supervisor.py:252` (`_Entry.last_exception`)

Stored as the raw exception object (not a formatted string), which keeps
`__traceback__` → frames → local variables reachable until the *next*
failure of that task — potentially months for a stable task. The only
consumer (`get_health_details`, `companion/services/router.py:340-348`)
already formats it as `f"{type(t.last_exception).__name__}: {t.last_exception}"`.

**Recommendation:** store the formatted string (or explicitly drop
`__traceback__`) at capture time in `_supervise` (`supervisor.py:244-252`).
Cheap, and the right shape for a process meant to run for months.

### PERF-06 — `GET/POST /api/v1/volume` inherits a 20 s wait meant for power commands
**Severity:** P2 · **Status:** OPEN · **Where:** `companion/services/router.py:517-529`, `partyboxd/device/manager.py:342-363` (`_get_connected_device`)

Related to **[ARCH-06](01-architecture-review.md#arch-06)** (accepted risk
for `POST /power/on`'s ~20 s block) but this is a *read* path picking up the
same wait unintentionally: `manager.get_volume()`/`set_volume()` both call
`_get_connected_device()`, which exists specifically so a power command
lands in an ADR-034 reconnect window instead of failing instantly. A
disconnected speaker therefore makes `GET /api/v1/volume` — a read with a
documented software-fallback path already available
(`VolumeState`) — hang for 20 s before falling back. The Portal doesn't hit
this (it doesn't call these endpoints on the hot path), but anything polling
it externally (Home Assistant, a script) pays 20 s per call and can stack
concurrent hung requests.

**Recommendation:** give reads a non-waiting variant (fail fast to the
`NotConnectedError`/software-fallback path immediately); reserve the
reconnect wait for the two power endpoints it was built for.

### PERF-07 — Steady-state log volume from routine retry-loop cycles
**Severity:** P3 · **Status:** OPEN · **Where:** `partyboxd/device/manager.py:520,845` (per-scan-cycle INFO), `companion/services/audio.py` (per-failed-attempt INFO/WARNING)

While the speaker is off, the scan loop logs 2 INFO lines per cycle and
AudioService logs INFO/WARNING per failed connect attempt — on the order of
4-5k journald lines/day of steady-state noise, all landing on SD flash.
`audio.py:359-366`'s `_gate_active` enter/exit-once pattern is the right
template (log the transition, not every re-evaluation) and is already used
for the standby gate; it isn't yet applied to the scan loop or the A2DP
failure ladder.

**Recommendation:** demote the per-cycle scan/retry lines to DEBUG, and log
state transitions (entered/left "no speaker found", entered/left "A2DP
failing") the way the standby gate already does.

### PERF-08 — Idle-battery-shutdown polls faster than its own thresholds need
**Severity:** P3 · **Status:** OPEN · **Where:** `companion/__main__.py:207,281` (`_IDLE_SHUTDOWN_CHECK_INTERVAL`)

15 s cadence to detect thresholds of 90 s (`"off"`) and 30 min (`"standby"`/
`"unreachable"`). The docstring's reason for polling at all instead of being
purely event-driven (charging-status changes don't emit
`SpeakerStateChangedEvent`) is legitimate and unrelated to this finding — but
the specific interval loses nothing meaningful at 60 s given the thresholds
it's compared against.

**Recommendation:** 15 s → 60 s. Negligible individually; consistent with
PERF-07's theme of trimming wakeups that don't buy detection latency anyone
would notice.

### PERF-09 — Pi 5 halt draw not reduced by `POWER_OFF_ON_HALT`; ADR-038's own acknowledged gap
**Severity:** P2 · **Status:** OPEN · **Where:** `image/install.sh`, `docs/adr/038-idle-battery-shutdown.md:51`

ADR-038 itself notes: *"the halted Pi still draws its own quiescent current
from the still-live USB rail... if it turns out to be more significant than
assumed, the only further reduction available in software is already
exhausted."* On a Pi 5 specifically, that quiescent draw is well-documented
as substantial (~1.2-1.4 W) **unless** `POWER_OFF_ON_HALT=1` is set in the
EEPROM config — which nothing in `image/` currently sets. Until it is, a
speaker left on battery with a "shut down" Pi still trickles power out of
its own pack toward the BMS's low-voltage cutoff, undermining exactly what
ADR-038 shipped to prevent.

**Recommendation:** add `POWER_OFF_ON_HALT=1` to the EEPROM config step in
`image/install.sh` (or the equivalent `rpi-eeprom-config` invocation) and
measure the actual quiescent draw before/after — closing the ADR's own
open question rather than leaving it as an assumption.

### Related, not new — cross-references only

- **WS `merged` queue is unbounded** (`partyboxd/api/ws.py:113`) — confirmed
  independently in this pass; identical to **[DEBT-11](02-technical-debt.md#debt-11)**.
  No new finding; the existing remediation stands.
- **No rate limit on power commands specifically** — **[SEC-07](04-security-review.md#sec-07)**
  already covers the general "no rate limiting on any endpoint" gap. Worth
  noting when SEC-07 is fixed: `/power/on`/`/power/off` already self-limit to
  roughly one cycle per ~40 s via the ADR-034 reconnect wait, so the
  hardware-wear exposure specifically is small — but an explicit cooldown
  (matching the shape of `request_adapter_reset`'s existing debounce) would
  close even that residual gap cheaply when SEC-07's fix lands.

---

## Part C — Future (Zero 2 W validation checklist)

Not findings against current code — a punch list for whoever validates the
Pi Zero 2 W target ARCH-01 notes has "never been measured there":

1. **Passive LE scanning mode** on the BlueZ backend for both DeviceManager's
   discovery scans and `_fddf_scan` — the FDDF service data is confirmed
   present in passive scans (per the 2026-07-18 JADX/live-capture
   cross-validation), and passive scanning eliminates SCAN_REQ transmissions
   entirely, a direct radio-time and power saving on a battery-adjacent
   board.
2. **Second-stage empty-scan backoff** — the current 8 s/60 s-capped scan
   cycle is a defensible ~12% duty cycle while genuinely searching, but after
   an extended empty period (say, an hour), stepping the cap up further
   (e.g. 5 min, reset immediately by any beacon sighting) would cut overnight
   idle scan radio time substantially with no discovery-latency cost anyone
   would notice.
3. **`bluetoothd` device-cache growth over months** — continuous discovery in
   a dense RF environment accumulates `Device1` objects and
   `/var/lib/bluetooth/*/cache/` entries for every device that ever passed
   by, growing bluetoothd RSS and SD writes over a multi-month uptime. The
   FDDF discovery filter (PERF-04) helps; a periodic cache prune does not
   yet exist anywhere in the codebase.
4. **Default librespot bitrate on constrained builds** — `SpotifySettings.bitrate`
   defaults to 320 (`companion/config.py`); Vorbis 320kbps decode + SBC
   encode is a real fraction of one Zero 2 A53 core. Worth a lower default
   (160) specifically for that target, configurable as today.
5. **Cold-boot import cost** — FastAPI + pydantic + bleak + dbus-fast import
   time, measured on the actual target board (`python -X importtime`), not
   assumed from Pi 5/Pi 3 experience. Combined with the unit's
   `ExecStartPre` chain (hci reset, 5 s fixed sleep, PipeWire restart,
   up-to-15 s endpoint wait), total time-to-Portal on a cold Zero 2 boot is
   currently unmeasured and could plausibly exceed a minute.
