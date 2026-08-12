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
- Access: `ssh pi@partybox.local`, once SSH is enabled and a key added via the
  Portal (Settings → SSH access) — SSH ships disabled by default with no
  fixed password (ADR-043); see CLAUDE.md for the one-time enable flow.
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
- Volume probes (VAL-VOL) — read all three stages, never just one:
  - stage 1: `pgrep -a librespot` (the live `--volume-ctrl` / `--initial-volume`
    / `--volume-range` flags). librespot's own mixer lines do **not** reach the
    journal at the default level, so the slider's current position is not
    directly observable — infer it, or raise `COMPANION_LOG_LEVEL`.
  - stage 2: `XDG_RUNTIME_DIR=/run/user/1000 wpctl get-volume @DEFAULT_AUDIO_SINK@`
    (expect `Volume: 1.00`), and `GET /volume`
  - stage 3: `sudo -u companion /opt/partybox-companion/bin/python -m \
    companion.services._a2dp_connect <MAC> volume` → `0-127` or `none`;
    `… <MAC> volume=<n>` to set; `… <MAC> state` for the transport
    (`active` / `idle` / `none`)
  - floor actions: `journalctl -u companion | grep -i avrcp`

**AVRCP caveat:** stage 3 reads come from BlueZ's cache, not the speaker. It can
be absent (before the peer's first notification), stale (holding a previous
session's write after a reconnect), or a low placeholder on a fresh transport.
Treat a read as evidence only when it is stable and no write or reconnect
happened in the preceding ~10 s.

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

### VAL-VOL — Volume chain

Audible output is the product of **three** independent gain stages. Most volume
defects in this project's history were one stage being wrong while the other two
looked fine, so every scenario below records all three, not just the symptom.

| Stage | What | Controlled by | Lossless? |
|---|---|---|---|
| 1 | Spotify slider → librespot softvol | `services.spotify` flags (`--volume-ctrl`, `--initial-volume`, `--volume-range`) | No — float gain then S16 quantise, ~1 bit per 6 dB |
| 2 | PipeWire A2DP sink node | `services.pipewire_volume`, pinned to unity | No — same quantise applies |
| 3 | Speaker amplifier (AVRCP absolute volume) | `services.avrcp_volume` floor + the speaker's own knob | **Yes** — acts after every digital stage |

Loudness headroom must always be taken from stage 3. Stage 1 at slider 100% is
already unity and bit-transparent; there is nothing above it to reach without
clipping.

#### Calibration (subjective — requires ears)

| ID | Level | Scenario |
|---|---|---|
| VOL-01 | M | **Startup level.** Fresh librespot start with the slider at `--initial-volume`: audible level is a comfortable default, neither startling nor apparently broken. *Why:* this value applies on **every** librespot start — crash respawns and service restarts included, not once a day — so too low a value makes every respawn read as a fault. Set to 35 once and reverted the same day for exactly that reason. |
| VOL-02 | M | **Slider extremes.** 1% is audible but genuinely quiet; 100% is loud enough for the room; 0% is true mute. *Why:* librespot special-cases only exact zero as mute (`mappings.rs`), so the taper's floor is what 1% actually delivers — at `--volume-range 15` that floor was ~17.8%, which made quiet listening impossible. |
| VOL-03 | M | **Slider sweep is monotonic and useful.** Sweep 0 → 100 in ~10 steps: loudness rises monotonically with no dead zone, and the lower half is usable for quiet listening. *Why:* a compressed range makes most of the slider's travel indistinguishable — the defect behind the range 15 → 30 change. |

#### Amp floor (stage 3)

| ID | Level | Scenario |
|---|---|---|
| VOL-04 | S | **Speaker power-cycle, knob below baseline.** Turn the knob well below `amp_baseline`, power-cycle the speaker: on reconnect the floor raises the amplifier to the baseline and logs one INFO line. *Why:* the core INC-2 mechanism. The speaker persists its **knob** position across a full power cycle (verified: knob 20 → power-cycle → reported 20), so without the floor the appliance inherits whatever the last person left. |
| VOL-05 | S | **Speaker power-cycle, knob above baseline.** Knob clearly above `amp_baseline`, power-cycle: the floor leaves it alone, no write logged, level stays where the operator put it. *Why:* the knob is the operator's way up (ADR-022 last-write-wins). A floor that lowered would fight them on every reconnect, audibly. |
| VOL-06 | A | **Standby → on (shorter than the Spotify grace).** `POST /power/off` then `/power/on` inside `_AUDIO_GRACE_SECONDS`: amp restored to baseline, librespot **not** restarted, slider position preserved. *Why:* the common case, and it must not silently reset the user's slider. |
| VOL-07 | A | **`systemctl restart companion` with the link up.** Floor fires on the resulting reconnect; all three stages correct afterwards. |
| VOL-08 | A | **`systemctl restart bluetooth` / adapter reset.** A2DP drops and re-establishes; the floor fires again on the new transport. *Why:* both are documented operator recovery levers. |
| VOL-09 | A | **5× consecutive A2DP reconnects.** Floor fires every time, level correct after each, and the time-to-correct does not degrade cycle over cycle. |

#### Untrustworthy reads (regression tests — all three have bitten us)

`MediaTransport1.Volume` is BlueZ's **cache**, updated on the peer's AVRCP
notification. It is not a query, and it has failed in three distinct ways.

| ID | Level | Scenario |
|---|---|---|
| VOL-10 | A | **Stale cache.** Set the amp to exactly `amp_baseline` by hand, force a reconnect, and confirm the floor **still writes** (INFO line present, not a silent skip). *Why:* regression for a live defect — after a reconnect the cache held the previous session's 52 while the speaker had reverted to the knob's 20, so "already at baseline" was indistinguishable from stale and the floor skipped. Hence the strictly-greater comparison. |
| VOL-11 | A | **Absent property.** On a fresh connect the floor either succeeds or logs its bounded-retry give-up at INFO — it must never skip silently. *Why:* BlueZ creates `Volume` only after the peer's first volume notification, which can lag the AVDTP transport that `_POST_CONNECT_SETTLE` waits for. |
| VOL-12 | A | **Placeholder read.** Record the value the floor reads on a fresh transport (observed: `8`/127, twice consecutively). Confirm it produces a correct raise, and treat any placeholder reading *above* `amp_baseline` as a FAIL. *Why:* a low placeholder is harmless because it raises; a high one would cause an incorrect skip and is the one unhandled case. |

#### Mid-session stability (negative tests — these justify *not* adding maintenance)

| ID | Level | Scenario |
|---|---|---|
| VOL-13 | A | **No time-based drift.** Set the amp above baseline, play continuously for ≥8 min with zero A2DP reconnects, sampling every 15 s: the value must not move. *Why:* a periodic re-assert was implemented and reverted because this test showed no drift across 28 samples. A FAIL here re-opens that design question. |
| VOL-14 | M | **Stream teardown and restart.** Stop playback until the transport reads `idle` (needs minutes, not a short pause — `node.pause-on-idle = false` keeps the node alive), then resume: the amp value is preserved. *Why:* the "speaker re-asserts on stream transition" hypothesis was tested and disproven. A FAIL means the speaker does re-assert and the floor needs a transport-state hook. |
| VOL-15 | M | **Knob-down mid-session is not corrected.** Turn the knob below baseline during playback: it stays down until the next reconnect. *Why:* documented intended behaviour, recorded so it is not mistaken for a defect. Nothing polls stage 3. |

#### librespot lifecycle and the slider

| ID | Level | Scenario |
|---|---|---|
| VOL-16 | M | **librespot respawn resets the slider.** Force a respawn (`POST /spotify/restart`): librespot returns at `--initial-volume`, while the Spotify app may still *display* its previous position. Confirm by nudging the slider down and back up. *Why:* cost an hour of misdiagnosis — dragging a slider that already reads 100% to 100% sends no event, so the app and librespot silently disagree. |
| VOL-17 | M | **Speaker away, under the grace period.** librespot survives, slider position preserved, only the amp is restored on return. |
| VOL-18 | M | **Speaker away, past the grace period.** librespot is torn down and deregisters from Spotify; on return it re-registers at `--initial-volume`. |
| VOL-19 | M | **Does the Spotify app override `--initial-volume`?** Set a distinctive value, restart librespot, reconnect from an app that previously set a different volume, and determine whether ours or the app's remembered value wins. *Why:* **open question.** Never observed, never ruled out — librespot logs its mixer line only when a client sets volume, and those lines do not reach the journal at the default level. If the app wins, the startup value is not ours to control and VOL-01 is unenforceable. |

#### Quality invariants

| ID | Level | Scenario |
|---|---|---|
| VOL-20 | A | **Sink pinned to unity.** `wpctl get-volume @DEFAULT_AUDIO_SINK@` is `1.00` after every fresh A2DP connect. *Why:* INC-2 stage 1 — WirePlumber defaults every new A2DP sink to `0.064` linear (~40% perceived). |
| VOL-21 | A | **No digital boost anywhere.** `POST /volume` clamps to 0–100 and the sink never exceeds `1.00`. *Why:* above unity clips rather than getting louder. |
| VOL-22 | A | **Slider 100% is bit-transparent.** With the slider at 100% and the sink at unity, no digital attenuation is applied at any stage. *Why:* the quality guarantee that lets loudness come from the amplifier instead of digital gain. |

#### Robustness and log quality

| ID | Level | Scenario |
|---|---|---|
| VOL-23 | A | **Speaker absent.** With the speaker off, the floor skips gracefully, logs at INFO, and does not crash the connect loop or spawn a subprocess storm. |
| VOL-24 | A | **Volume API still reflects stage 2.** `GET /volume` reports the sink level and source; `POST /volume` changes it. *Why:* the Portal slider and Home Assistant both depend on this, and it drives the sink, *not* AVRCP. |
| VOL-25 | A | **Floor log wording.** A floor action logs exactly one INFO line, worded as a *request* ("requested … BlueZ accepted, applied asynchronously"), never as a confirmed level; a legitimate skip is DEBUG; a bounded-retry give-up is INFO. *Why:* this line is read while debugging volume complaints and must not assert a level nobody verified. |

#### How not to fool yourself

Hard-won during the 2026-08-12 session, where three separate volume bugs and one
methodology error were in play simultaneously.

1. **Change one stage at a time and hold the others fixed.** Two subjective
   judgements made at different slider positions are not comparable. Believing
   otherwise produced a "too loud at all levels" report that was really a
   quieter-than-before setting, and cost an hour.
2. **Record the slider position *and* the amp value beside every subjective
   judgement.** "It sounds right" without both numbers is unusable later.
3. **Never trust an AVRCP read-back within ~10 s of a write.** BlueZ echoes the
   cached write; the speaker applies asynchronously and then re-notifies. A fast
   read-back once produced a confident, wrong "the speaker ignores AVRCP".
4. **Prove no reconnect happened** during any observation window
   (`journalctl -u companion | grep -c "A2DP connection established"`). A
   reconnect-driven revert is expected and will be mistaken for drift.
5. **Re-check the slider after any service restart** before judging loudness —
   librespot came back at `--initial-volume`, whatever the app displays.
6. **A pause is not a stream teardown.** `node.pause-on-idle = false` keeps the
   transport `active`; reaching `idle` takes minutes of genuine silence.

Practical traps when scripting on the appliance:

- `busctl --list tree org.bluez` can hang for minutes under load and has killed
  SSH sessions. Use `python -m companion.services._a2dp_connect <MAC> volume`
  instead — it finds the transport itself.
- `pkill -f <script>` over SSH matches the SSH command line too and kills its
  own session. Match on a bracketed pattern or kill by recorded PID.

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
4. Reversible event scenarios: SPKR-01…05, HOST-01…04, API-03, FAULT-01/06,
   VOL-06…12 and VOL-20…25 (the automatable volume set — run VOL-20 early, since
   a sink that is not at unity invalidates every subjective judgement after it).
5. Reboot-class scenarios: BOOT-03/04/05, HOST-05, FAULT-02.
6. Riskier fault injection: FAULT-03/04/05, NET-03.
7. Long-running: STREAM-01/04, RES-02, SOAK-01, then SOAK-02 overnight.
7b. VOL-13 (8-min no-drift hold) alongside the other long-running scenarios.
8. Human-required batch (schedule with the operator in one session):
   BOOT-01 re-run if needed, BT-01/02/03, STREAM-02/03, FAULT-05, and the
   volume set VOL-01…05, VOL-14…19 — calibration first, then the physical
   power-cycle pair, then the librespot-lifecycle ones. Doing them in one
   sitting matters: the calibration scenarios are only comparable to each
   other if the amp and the slider are held fixed between them.
9. LOG-01/02/03 throughout, consolidated at the end.

## Maintaining this suite

- New field failure → new scenario (or tightened expected-behaviour) here.
- Scenario obsoleted by architecture change → delete it, note why in the
  removing commit.
- Keep scenarios *behavioural* (what a user/operator would observe), not
  implementation-coupled, so refactors don't invalidate the suite.
