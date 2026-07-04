# Appliance Validation Suite

The canonical regression suite for the PartyBox Companion appliance. Every
release candidate must be validated against this document before it can be
promoted to a release.

This document defines **what to test and why**. The results of executing it
against a specific image live in per-release run reports under
[`runs/`](runs/) (e.g. `runs/2026-07-02-rc13.md`). The spec evolves
independently of any single release: when a new failure mode is discovered,
add a scenario here so every future release is checked for it.

---

## Philosophy

The goal is not to confirm that things work. The goal is to prove the
appliance behaves **predictably** — and to surface behaviours that surprise
us. A scenario that passes but produces unexpected log output is a finding,
not a pass.

Rules of engagement:

1. **Evidence over assertion.** Every verdict cites collected evidence:
   journal excerpts, API responses, timing measurements, `bluetoothctl` /
   `wpctl` state dumps. "It seemed fine" is not a result.
2. **Automate everything automatable.** The appliance is fully drivable
   remotely (SSH, REST API, systemctl, journalctl, nmcli, wpctl, BLE power
   control). Human hands are reserved for physically unpluggable things.
3. **Verify the whole causal chain.** When a scenario recovers, confirm each
   link (event observed → detection logged → recovery action → healthy state),
   not just the final state. A system that recovers by accident will fail by
   accident.
4. **Unexpected observations are first-class results.** Record them in the
   run report with an `OBS-n` identifier even when the scenario passes.
5. **Logs are a product surface.** Noise, misleading warnings, and
   undocumented sequences are defects (see VAL-LOG).

### Verdicts

| Verdict | Meaning |
|---|---|
| **PASS** | Observed behaviour matches expected behaviour; evidence attached |
| **PASS (obs)** | Passed, but with unexpected observations that need follow-up |
| **FAIL** | Expected behaviour not met; must be fixed or explicitly waived with rationale |
| **BLOCKED** | Could not execute (missing prerequisite); reason recorded |
| **DEFERRED** | Intentionally not run this cycle; rationale recorded |

### Run report format

Each scenario execution records:

```
### <ID> — <title>                                    <VERDICT>
- Expected: …
- Observed: …
- Evidence: (log excerpts, API responses, timings)
- Follow-up: (or "none")
```

---

## Environment & instrumentation

- Appliance: Raspberry Pi flashed with the release-candidate image, WiFi
  provisioned via captive portal.
- Access: `sshpass -p raspberry ssh pi@partybox.local` (see CLAUDE.md).
- REST base: `http://partybox.local/api/v1` (unauthenticated by default).
- Key probes:
  - `GET /health` → `{status, version, ble_connected, audio_ready}`
  - `GET /speaker`, `GET /spotify`, `GET /audio`, `GET /wifi/status`
  - `journalctl -u companion` (service logs; journald is volatile — collect
    evidence **before** rebooting)
  - `bluetoothctl info <MAC>` (BLE + BR/EDR link state)
  - `wpctl status` / `pw-cli ls Node` (PipeWire sink state)
  - `systemd-analyze`, `systemd-analyze critical-chain companion.service`
  - `free -m`, `ps -o rss,vsz,etime -p $(pgrep -f companion)`, `top -bn1`

**Timing convention:** recovery times are measured from the injected event to
the first healthy probe result, polling at 1–2 s intervals. Report median-ish
single figures honestly (e.g. "~12 s, polled at 2 s resolution").

**Journal caveat:** the Pi has no RTC; journal lines written before NTP sync
carry a stale fake-hwclock date. Correlate early-boot events by monotonic
offset (`journalctl -o short-monotonic`) rather than wall-clock time.

---

## Scenario catalog

Automation levels: **A** = fully automatable (Claude executes end-to-end),
**S** = semi (needs one physical action, e.g. pressing the speaker's pairing
button or unplugging mains power), **M** = manual/human (phone interaction,
physical relocation).

### VAL-BOOT — Cold boot & first-run

| ID | Level | Scenario |
|---|---|---|
| BOOT-01 | S | **First boot / captive portal provisioning.** Fresh image, no WiFi credentials: boots into AP mode (`PartyBox Companion Setup`, 10.42.0.1), wildcard DNS triggers the OS captive-portal popup, network selection + join succeeds, AP torn down, Portal reachable at `http://partybox.local`. *Why:* the first five minutes of ownership; a regression here bricks onboarding for non-technical users. *Evidence:* provisioning log sequence, `wifi/status` transitions, retrospective journal review. |
| BOOT-02 | S | **Fresh A2DP pairing.** Never-bonded speaker: `POST /audio/pair` discovers the BR/EDR address from the FDDF LE advertisement, pairs, trusts, connects, persists `audio_sink_address` to config, and `audio_ready` goes true without a service restart. Attempt first by exploiting the post-power-on pairing window (power-cycle via BLE, then pair immediately); fall back to physical pairing-mode button. *Why:* the ADR-027 `Agent1` flow had never been hardware-verified before RC13; it is the one-shot gate to all audio. |
| BOOT-03 | A | **Cold boot, speaker already on (bonded).** Full reboot with speaker on: BLE reconnects, A2DP reconnects, `audio_ready: true`, librespot registers — all without intervention. Record time-to-healthy. *Why:* the most common power-restoration scenario (e.g. after a power cut both devices return together). |
| BOOT-04 | A | **Cold boot, speaker off.** Reboot with the speaker off (via BLE power-off pre-reboot): appliance reaches steady idle state (Portal up, scanning at a calm cadence, Spotify hidden), then speaker power-on leads to full recovery. *Why:* proves the appliance idles predictably instead of thrashing when the speaker is absent. |
| BOOT-05 | A | **Repeated reboot loop (≥5×).** Consecutive `sudo reboot` cycles; after each: `ble_connected`, `audio_ready`, Spotify registration, Portal reachable. *Why:* catches nondeterministic startup races that a single boot hides (service ordering, BT controller init, WirePlumber attach). |
| BOOT-06 | A | **Service startup ordering.** `systemd-analyze critical-chain companion.service`; confirm companion starts after `bluetooth.service` and `network-online.target`, and that no unit failed (`systemctl --failed`). *Why:* ordering bugs manifest as rare boot failures in the field. |

### VAL-SPKR — Speaker lifecycle

| ID | Level | Scenario |
|---|---|---|
| SPKR-01 | A | **Off → On via REST.** `POST /power/on` from standby: BLE stays/reconnects, A2DP connects within ~5 s, `audio_ready: true`, librespot (re)registers. Record timings and the log sequence. |
| SPKR-02 | A | **On → Off via REST.** `POST /power/off`: A2DP drops cleanly, `audio_ready: false`, Spotify visibility withdrawn after the grace period, no error-level noise, no reconnect storm while off. |
| SPKR-03 | A | **5+ consecutive power cycles.** Alternate off/on with settle time; verify recovery after *every* cycle and that recovery time does not degrade cycle-over-cycle. *Why:* WirePlumber endpoint-flap history (ADR-028) — degradation accumulates across cycles, not within one. |
| SPKR-04 | A | **Rapid REST power toggling.** on/off/on with ~2 s gaps, then verify convergence to the final commanded state. *Why:* a user mashing the Portal button must not wedge the state machine mid-transition. |
| SPKR-05 | A | **Long speaker-off period (> Spotify grace).** Speaker off for > the deregistration grace period: librespot deregisters, appliance idles quietly (bounded scan cadence, no log spam), then recovers on power-on. *Why:* overnight-off is the default consumer state; log volume while idle is an SD-wear and diagnosability concern. |
| ~~SPKR-06~~ | — | **Out of range / return — descoped.** Not relevant for this appliance: the Pi and speaker are a co-located fixed install (typically the same room/enclosure), so BLE range loss is not a realistic consumer scenario. Its distinct code path — supervision timeout rather than clean disconnect — is already exercised whenever the speaker is powered off with the appliance still connected (SPKR-02/05, and observed repeatedly in the RC13 run's day-scale reconnect churn). Kept as a strikethrough row rather than deleted so it is not silently re-added. |

### VAL-HOST — Raspberry Pi lifecycle

| ID | Level | Scenario |
|---|---|---|
| HOST-01 | A | **`systemctl restart companion`.** Clean shutdown (no errors in stop sequence), full recovery: BLE, A2DP, `audio_ready`, Spotify. Record time-to-healthy. |
| HOST-02 | A | **10× repeated companion restarts.** Back-to-back restarts; verify recovery every time and no resource leakage (BlueZ device state, PipeWire nodes, orphan librespot processes). *Why:* restart is the documented workaround for several issues; it must be unconditionally safe. |
| HOST-03 | A | **`systemctl restart bluetooth` while companion runs.** Companion detects the dropped BLE + A2DP links, logs a clear diagnostic, and recovers without a companion restart. *Why:* operators will do this; docs recommend it for GATT failures. |
| HOST-04 | A | **WirePlumber restart while companion runs.** `systemctl --user -M pi@ restart wireplumber`: audio graph rebuilds, A2DP sink reappears, `audio_ready` recovers. *Why:* this is AudioService's own recovery lever — it must be safe when fired externally too. |
| HOST-05 | A | **Reboot with active state.** (Covered by BOOT-03/05 but verify shutdown side): reboot while connected — clean service stop, no shutdown hang, no unit timeout. |

### VAL-BT — Bluetooth contention

| ID | Level | Scenario |
|---|---|---|
| BT-01 | M | **Phone owns A2DP before Pi.** Speaker connected to a phone as A2DP source, then companion starts: graceful retry, clear diagnostics, no crash loop; recovery when phone disconnects. |
| BT-02 | M | **Phone connects while Pi is connected.** JBL supports multipoint-ish behaviour; observe and document what actually happens (audio stolen? both connected? BLE unaffected?). *Why:* undefined behaviour today — the goal is to characterize it. |
| BT-03 | M | **JBL app / third-party BLE central.** Confirm the documented v1.0 limitation (exclusive BLE central) presents sanely: JBL app can't connect while companion runs; companion recovers if it briefly loses BLE to another central. |

### VAL-STREAM — Streaming & audio stability

| ID | Level | Scenario |
|---|---|---|
| STREAM-01 | A | **30-min continuous A2DP stream (synthetic).** `pw-play` / `pw-cat` a generated tone from the Pi to the sink for 30 min; verify zero xruns (`pw-top`), no BLE drops, no endpoint flap, stable `audio_ready`. *Why:* closes the extended-run item deferred since M3 without needing a Spotify account in the loop. |
| STREAM-02 | M | **30–60 min real Spotify playback.** Human starts playback from a Spotify client; verify same stability criteria plus librespot event handling (play/pause/skip). |
| STREAM-03 | M | **Pause/resume + repeated skips.** Exercise librespot event churn; verify no state desync between `spotify` endpoint, logs, and audible behaviour. |
| STREAM-04 | A | **Idle → resume.** After ≥1 h fully idle (connected, no stream), start a synthetic stream; verify no WirePlumber endpoint degradation (the ADR-028 regression check). |

### VAL-FAULT — Fault injection

| ID | Level | Scenario |
|---|---|---|
| FAULT-01 | A | **Kill librespot (SIGKILL).** Supervisor restarts it with backoff; Spotify re-registers; failure surfaced in diagnostics, not swallowed. |
| FAULT-02 | A | **Kill companion (SIGKILL).** systemd `Restart=on-failure` brings it back; full recovery; no stale BlueZ/PipeWire state blocks the new instance. |
| FAULT-03 | A | **WiFi loss & recovery.** Drop WiFi via `nmcli` with a scheduled re-up (systemd-run, since SSH rides the same link); verify BLE/A2DP unaffected during the outage, mDNS + Spotify Zeroconf return after recovery. *Why:* router reboots are routine; audio should survive them. |
| FAULT-04 | A | **Corrupted config.json.** Write invalid JSON to `/var/lib/companion/config.json`, restart: appliance must start with defaults (or a clearly-diagnosed error), never crash-loop. Restore config afterwards. |
| FAULT-05 | A | **Stale A2DP bond.** `bluetoothctl remove` the bonded BR/EDR device while config still points at it: AudioService's connect must fail with a clear diagnostic (not silent retry-forever), and `POST /audio/pair` must recover. *Why:* users re-pair speakers with other devices; bonds go stale in the field. |
| FAULT-06 | A | **Rapid API abuse.** Concurrent/rapid REST calls (power toggles, config writes, pair-while-pairing → 409); verify no 500s, consistent final state. |

### VAL-NET — Network & discovery

| ID | Level | Scenario |
|---|---|---|
| NET-01 | A | **mDNS stability.** `partybox.local` resolves repeatedly (≥20 probes over several minutes), including during active A2DP streaming (2.4 GHz coexistence — known Pi 3 B+ limitation; on other hardware this must pass). |
| NET-02 | A | **Spotify Zeroconf visibility.** `_spotify-connect._tcp` advertised (via `avahi-browse` from the Pi) exactly when `audio_ready` is true. |
| NET-03 | A | **WiFi reconnect.** After FAULT-03 style outage: NM rejoins automatically, no provisioning-mode false trigger (AP must **not** come up during a transient outage). *Why:* the provisioning trigger ("no active WiFi") firing on a transient outage would take the appliance off-network. |

### VAL-RES — Resources & soak

| ID | Level | Scenario |
|---|---|---|
| RES-01 | A | **Baseline resource snapshot.** RSS, CPU, FD count, thread count of companion + librespot at steady idle and during streaming; recorded per release for trend comparison. |
| RES-02 | A | **Memory growth over event churn.** Sample RSS before/after the power-cycle and restart batteries; flag monotonic growth. |
| SOAK-01 | A | **2 h unattended soak.** Periodic health/RSS/journal-error sampling; no accumulating failures, no reconnect loops, memory flat. |
| SOAK-02 | A | **Overnight soak (8–12 h).** Same sampling at lower frequency. *Why:* slow leaks and rare-event accumulation (RPA rotation, NM lease renewals) only show at this scale. |

### VAL-LOG — Log quality (product surface)

| ID | Level | Scenario |
|---|---|---|
| LOG-01 | A | **Boot sequence review.** Document the canonical happy-path startup log sequence; every WARNING/ERROR during a clean boot must be actionable or eliminated. |
| LOG-02 | A | **Recovery sequence review.** Document canonical log sequences for: speaker power cycle, bluetooth restart, companion restart. Verify no misleading errors (e.g. scary tracebacks for expected disconnects). |
| LOG-03 | A | **Noise audit.** Rank messages by frequency over a soak window; any line repeating unboundedly at INFO+ during steady state is a defect (SD wear is design constraint — journald is volatile partly for this reason). |

### VAL-API — REST API regression

| ID | Level | Scenario |
|---|---|---|
| API-01 | A | **Endpoint sweep.** Every documented `/api/v1/*` endpoint returns the documented shape and status code in both speaker-on and speaker-off states (incl. 503s, 404 battery on mains models). |
| API-02 | A | **Error shape consistency.** All errors match `{"detail": {"error", "message"}}`. |
| API-03 | A | **WebSocket events.** `/api/v1/events` delivers `connected`/`disconnected`/`power_changed` matching injected power transitions, with no duplicates or ghosts. |
| API-04 | A | **Auth toggle.** With `api_key` configured: protected endpoints 401/403 without key, work with key, `/health` stays public. Restore config afterwards. |

---

## Execution order (recommended)

1. **Retrospective first**: BOOT-01 evidence from the live journal *before*
   any reboot (journald is volatile — a reboot destroys it).
2. BOOT-02 fresh pairing (unlocks everything audio-related).
3. Non-destructive state probes: API-01/02, NET-01/02, RES-01, BOOT-06.
4. Reversible event scenarios: SPKR-01…05, HOST-01…04, API-03, FAULT-01/06.
5. Reboot-class scenarios: BOOT-03/04/05, HOST-05, FAULT-02.
6. Riskier fault injection: FAULT-03/04/05, NET-03.
7. Long-running: STREAM-01/04, RES-02, SOAK-01, then SOAK-02 overnight.
8. Human-required batch (schedule with the operator in one session):
   BOOT-01 re-run if needed, BT-01/02/03, STREAM-02/03, FAULT-05.
9. LOG-01/02/03 throughout, consolidated at the end.

## Maintaining this suite

- New field failure → new scenario (or tightened expected-behaviour) here.
- Scenario obsoleted by architecture change → delete it, note why in the
  removing commit.
- Keep scenarios *behavioural* (what a user/operator would observe), not
  implementation-coupled, so refactors don't invalidate the suite.
