# ADR-044: Surfacing 2.4 GHz Congestion as a Diagnosable Condition

**Status:** Accepted

---

## Context

A hardware troubleshooting session on 2026-09-01 chased intermittent music
stutter on a deployed appliance. Every signal the system already had said the
appliance was healthy:

- `GET /api/v1/health` — `ble_connected: true`, `audio_ready: true`,
  `speaker_state: "on"`, `audio_focus: "exclusive"`
- PipeWire — `ERR = 0` on every node; the A2DP sink used ~15-100 µs of its
  42.7 ms quantum, so frames were handed to the kernel on time, every time
- SoC — 50.5 °C, `throttled=0x0`: no thermal or undervoltage event
- The Portal health sheet — a green "Bluetooth Audio: Ready" row

The fault was entirely in the RF environment. Reading the controller's AFH
(Adaptive Frequency Hopping) map showed the link hopping across **20 of 79
channels** — `Nmin`, the floor the Bluetooth spec forbids a controller to go
below. Nine WiFi APs sat across channels 1, 6 and 11, and the excluded
spectrum matched their footprint almost exactly: 2402-2423 (channel 1) and
2451-2473 (channel 11). Corruption was visible in the kernel ring as
`Bluetooth: Unexpected continuation frame (len 0)` — L2CAP reassembly
failing on damaged packets.

Consolidating that network's 2.4 GHz radios onto channel 6 alone moved the
map to a stable **48 of 79 channels**, with the only remaining exclusion
being channel 6's own footprint (2422-2451). The stutter stopped.

Two gaps made this diagnosis need SSH and manual `hcitool` work:

1. **The debug bundle could not have shown it.** `GET /api/v1/debug/bundle`
   collects version, config, service status, device snapshot, platform, and
   `journalctl --unit=companion`. Nothing RF. The kernel L2CAP errors are
   not in the companion unit's journal, so the bundle carried the *symptom*
   — BLE reconnect churn — with nothing to separate "congested band" from
   "speaker out of range" from "failing adapter". This matters most in the
   case the bundle exists for: a user who cannot debug it themselves and
   sends it to someone who cannot reach the appliance at all.

2. **The Portal had no state between "Ready" and broken.** `healthItems()`
   models Bluetooth Audio as not-paired / pairing-failed / contested /
   Ready / connecting. A link pinned at the spec floor reports **Ready**,
   which is what the user saw while the music broke up.

## Decision

### 1. A congestion survey built on WiFi scanning, not on AFH

The direct measurement is the AFH map, via an HCI `Read AFH Channel Map`
command (OGF 0x05, OCF 0x0006) on a raw HCI socket. That needs `CAP_NET_RAW`
/`CAP_NET_ADMIN`. The `companion` user is a hardened system account with
`NoNewPrivileges`, `ProtectSystem=strict` and no sudoers grants at all
(ADR-019); every privileged operation it performs goes through D-Bus behind a
narrowly scoped polkit rule. Granting a raw-socket capability to the whole
service — which also runs an HTTP server on port 80 — to read one diagnostic
integer is not a trade this project should make.

The dominant interferer in practice is neighbouring 2.4 GHz WiFi, and
NetworkManager already enumerates it. ADR-021 installs polkit grants for
`org.freedesktop.NetworkManager.*` for provisioning, so **the survey needs no
new privileges whatsoever**. `companion/services/rf_survey.py` reads the scan
list and reduces it to per-channel AP counts plus a band-occupancy figure.

This is a proxy, not the measurement, and is treated as one: it reports that
the *environment* is hostile, never that the link is degraded. The appliance
does not claim to know the AFH map.

### 2. Occupancy is a spectral union, not an AP count

Ten APs stacked on channel 6 block precisely the spectrum one AP on channel 6
blocks; three APs spread across 1, 6 and 11 leave AFH almost nothing. A count
cannot tell those apart, and the second is the one that breaks audio — as the
capture above shows, where the fix was consolidating APs onto one channel
without removing any. So occupancy is the union of the 22 MHz footprints of
APs strong enough to matter, over the 2401-2483 MHz band, and `congested` is
that union exceeding 50%.

`_STRONG_SIGNAL = 65` (nmcli's 0-100 scale) is calibrated against the capture,
not taken from a spec: APs at signal 69-85 had their spectrum excluded from
the observed AFH map; APs at 60-64 on an otherwise-quiet channel did not.
Documented at its definition as the behavioural threshold it is.

### 3. The survey never triggers a scan

`nmcli ... --rescan no` reads NetworkManager's existing cache. Forcing a scan
makes the radio sweep every channel, which on a shared antenna interrupts the
A2DP stream — a diagnostic that causes the fault it reports, on a path polled
by the Portal every refresh. NetworkManager refreshes the cache on its own
schedule, far finer-grained than the timescale on which a neighbour's WiFi
configuration changes. This is asserted by a test, not just a comment.

### 4. No SSIDs or BSSIDs leave the survey

`GET /api/v1/rf` is unauthenticated, like the other coarse read-only routes.
ADR-037 drew that line at whether a response contains sensitive data, and a
list of neighbouring SSIDs is exactly what public wardriving databases index
to geolocate a device. The identifying half of the scan is therefore dropped
in `rf_survey.py` at the point of collection rather than filtered at the
serialisation boundary, so no future caller can reintroduce it by accident.
What remains — AP counts, channel numbers, an occupancy percentage —
identifies neither the neighbours nor the appliance's location.

`ProvisioningService.scan_networks` is deliberately **not** reused: it
de-duplicates by SSID, because a user joins a network rather than a radio.
Three APs sharing one SSID on one channel is the exact situation this module
exists to detect, so that dedupe would erase the signal.

### 5. `available: false` is not an all-clear

A missing scan list — no nmcli, radio in AP mode during provisioning, empty
cache — is reported as `available: false`, distinct from a survey reporting
zero APs. The Portal shows no row at all in that case rather than a clean bill
of health the appliance never observed.

### 6. Kernel lines join the debug bundle

`kernel.txt` carries `journalctl --dmesg`, filtered to
`Bluetooth|hci|brcmfmac|voltage|throttl|thermal`. The service user can already
read this through the `SupplementaryGroups=systemd-journal` grant
`_collect_journal_logs` relies on — again no new privileges.

The filter is deliberate rather than a plain tail: a Pi's boot-time kernel
spew alone exceeds the 500-line budget, so an unfiltered tail would reliably
push out the recent Bluetooth lines the bundle is being collected for. The
pattern also covers the two faults that are *indistinguishable from
interference* when viewed from userspace — a marginal PSU (`voltage`) and a
throttling SoC (`throttl`, `thermal`) both produce dropouts while every
service reports healthy.

### 7. Two graded halves, not one boolean

The survey answers "is the environment hostile". It cannot answer "is the
link actually suffering", and those come apart in both directions: a crowded
band with a quiet link needs no action, while a link failing on a clear band
means the cause is something a WiFi scan cannot see. Reporting only the first
would produce both false alarms and false silence.

So `GET /api/v1/rf` returns two independently graded halves plus a headline:

- `band` — environmental risk, from the WiFi survey
- `link` — faults actually observed, from `link_health`
- `level` — the worst of whichever halves were readable, `"unknown"` if
  neither was

**`link` deliberately does not use PipeWire xruns.** During the incident they
read **zero** throughout continuous audible stutter, because frames were
handed to the kernel on time and lost on the air afterwards. An "audio
health" built on the obvious metric would have shown green for the entire
fault. What did track it, from the same capture:

| Window | L2CAP errors | Rate |
|---|---|---|
| 14:05:33-14:09:30 (faulty) | 6 | ~90/hour |
| 14:09:30-17:07 (recovered) | 3 | ~1/hour |

with near-instant correlation to A2DP failures — a `br-connection-unknown`
at 14:09:07 and a kernel L2CAP error in the same second. Both counters are
read from the journal the service can already see; nothing is put on the air
to measure them, and the sample is memoised for 25 s so the Portal's 30 s
reconcile does not spawn processes continuously.

Thresholds (`_L2CAP_WARN`/`_L2CAP_ERR` = 5/30 per hour, `_A2DP_WARN`/`_ERR`
= 2/6) sit in the wide gap between those two measured rates. The honest claim
they support is "quiet" versus "clearly faulty", not a fine-grained quality
score, and they have seen exactly one RF environment.

Band occupancy grades green below 0.35, amber to 0.5, red above. One 22 MHz
WiFi channel is ~27% of the band and unavoidable wherever 2.4 GHz WiFi exists
at all, so a single channel must not warn. Note the scale is effectively
**quantised**: real APs cluster on channels 1, 6 and 11, so a typical
environment steps 0.28 → 0.55 → 0.83 and skips amber entirely. Amber is
reached only by APs on overlapping, non-standard channels. That is how WiFi
is deployed, not a gap in the thresholds.

### 8. The Portal row

Always present once a reading exists — like the Speaker/Bluetooth Audio/
Spotify rows either side of it. A green "checked, and it's fine" is itself
useful on a sheet whose whole job is to answer "is anything wrong?", and an
only-on-failure row is inconsistent with every sibling.

Link faults lead both the colour and the copy; the band is only ever the
*explanation*. The other order would headline a crowded band doing no harm
and bury a link failing for a reason the scan cannot see.

> **Signal — link errors.** 90 Bluetooth link errors and 7 dropped
> connections in the last hour — audio may break up. 9 nearby WiFi networks
> on channels 1, 6, 11 using 55% of the 2.4 GHz band is the likely cause —
> setting your WiFi to a single 2.4 GHz channel usually helps.

When the band is clear the copy says so plainly rather than implying WiFi
anyway, since microwaves, Zigbee hubs and plain distance are all invisible
here. No button either way: the fix is on the user's router or in the room,
not on the appliance.

### 9. `SPEAKER_UNREACHABLE` must not lead with a Bluetooth reset

`deriveScene()` routes to `BLOCKED` — which carries the correct "disconnect
the speaker on that phone" copy — only when `audio_focus` is *confirmed*
`contested`. Everything else with the control link down falls through to
`SPEAKER_UNREACHABLE`, whose copy offered a **Reset Bluetooth** button as the
next step.

That fall-through includes the case where a phone genuinely holds the
speaker but the FDDF watcher has not classified it yet, and that gap is not
small. On 2026-09-01 the user pressed Reset Bluetooth at 14:05:55; focus only
resolved to `contested` at 14:07:14, **79 seconds later**. The reset could not
have helped — it power-cycles the appliance's own adapter and cannot evict a
phone from the speaker — and cost about a minute of reconnection, visible in
the journal as scan/connect churn through 14:09.

Three changes, none of which require knowing the answer sooner:

1. The body is now derived from `audio_focus`. While it is `unknown` the copy
   leads with the phone check — both the cheaper action and the commoner
   cause — and says outright that Companion is still determining which case
   this is. Once focus is `exclusive`, we know no other device holds the
   speaker and the copy narrows to the appliance-and-speaker case.
2. Reset moves behind a "More options" disclosure instead of sitting as the
   obvious next step.
3. That disclosure states what the reset *cannot* do: "It can't disconnect
   another device from the speaker — that has to be done on the device
   itself."

## Explicitly out of scope

- **Reading the AFH map.** The privilege cost is not justified for v1. If a
  future need makes it worthwhile, the shape is a small privileged helper
  behind D-Bus + polkit, matching every other privileged operation here —
  not a capability grant on the main service. The survey's contract ("the
  environment is hostile") would not change; it would gain a second, direct
  input.
- **Historical trending.** The survey is instantaneous. "Your band got worse
  at 8pm every night" needs storage and a retention policy, and would be
  speculative before anyone has asked for it.
- **Zigbee, microwaves, USB 3 and analogue video senders.** All real 2.4 GHz
  interferers, none visible to a WiFi scan. The row deliberately says a
  crowded band *can* cause stutter rather than claiming to have found the
  only cause.
- **Acting on the survey.** No automatic codec downgrade or A2DP
  reconfiguration. SBC is already the most loss-tolerant codec available,
  and the fault is off-appliance.
- **Classifying `audio_focus` faster.** The obvious fix for §9's 79-second
  gap is to scan for the FDDF beacon when the control link first drops.
  `AudioFocusService` deliberately skips scanning outright while
  `ble_connected_fn` is False, because an investigation on 2026-07-18
  correlated this service's scan windows with `DeviceManager` connect
  failures — including a live ADR-039 adapter wedge triggered moments after
  one. Scanning during exactly the window the gate exists to protect would
  reinstate a documented incident to save a caption, so the copy carries the
  uncertainty instead. Closing the gap properly means coordinating the two
  scanners (the scan/connect mutex that parameter's docstring already
  considered and rejected on cost), which is a larger change than this ADR.
