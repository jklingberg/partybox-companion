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

### 7. The Portal row

A `warn` row, shown once a speaker is paired (connected or not — a crowded
band explains both audio breaking up and a link struggling to hold), naming
the channels and the occupancy percentage, and saying what to do:

> **2.4 GHz Band — crowded.** 9 nearby WiFi networks on channels 1, 6, 11 are
> using 55% of the 2.4 GHz band, which Bluetooth shares. This can make audio
> stutter — setting your WiFi to a single 2.4 GHz channel usually helps.

This follows the `audio_focus === 'contested'` row precedent: a condition
where every ordinary signal reads healthy, surfaced in plain language with
the action that resolves it. As with that row, no button is offered — the fix
is on the user's router, not on the appliance.

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
