# Hardware Validation — FAULT-05 stale A2DP bond recovery (2026-07-28)

Not a full release validation run: this closes the single outstanding
physical-action item from the RC13 run
([runs/2026-07-02-rc13.md](2026-07-02-rc13.md)), tracked as
[#77](https://github.com/jklingberg/partybox-companion/issues/77) and defined as
`FAULT-05` in [appliance-validation.md](../appliance-validation.md). RC13 left
it as the one scenario requiring an operator at the speaker:

> **FAULT-05** (stale A2DP bond recovery) — human: `bluetoothctl remove` then
> re-pair needs the pairing-mode button press; best run immediately before a
> planned re-pair so the appliance is left bonded.

**Scope correction.** #77 is titled around fresh `Agent1`/`Pair()` never having
been hardware-verified. That framing is out of date: RC13's `BOOT-02` ("Fresh
pairing happy path — **PASS**", 2026-07-03) already exercised the full
`Agent1` registration → `Pair()` → bond flow against a genuinely never-bonded
speaker. What remained unverified — and what this run covers — is `FAULT-05`:
the *stale bond* path, where BlueZ has forgotten the device but
`config.json` still points at it.

## Environment

| Item | Value |
|---|---|
| Image | `0.1.0rc15` (unmodified — no code deployed for this run) |
| Host | Raspberry Pi 5 Model B Rev 1.1, Debian 12 (bookworm), at `partybox` / 192.168.1.221 |
| Controller | BCM4345C0 (Cypress), `hci0` `88:A2:9E:96:D0:78`, UART/H4 |
| Speaker | JBL PartyBox 520, BR/EDR `50:1B:6A:14:FD:1D`, firmware 26.2.10, battery 100% (mains) |
| PipeWire | 1.2.7, `pi` session at `/run/user/1000` (per ADR-019) |
| WiFi | 5 GHz (`Klingbergs`, 5.22 GHz) — no 2.4 GHz coexistence with BT from the Pi's own radio |
| Access | SSH as `pi`, key provisioned via Portal → Settings → SSH access (ADR-043) |

Operator present at the speaker for the pairing-mode button press. All other
steps driven remotely.

## Method note: throughput as the audio-quality measure

Audible stuttering was quantified by sampling `hciconfig hci0` TX bytes over a
fixed window during playback, rather than by ear alone. For SBC 48 kHz stereo
the healthy range is ~25–45 KB/s. This proved decisive several times over: it
distinguished a genuinely starved link from a healthy one in cases where
PipeWire reported zero xruns, `hciconfig` reported `errors:0`, RSSI and link
quality were strong, and `/api/v1/health` reported everything green — i.e.
where **no existing health signal showed the fault at all**.

Playback was cross-checked with a locally generated `pw-cat` tone as well as
Spotify, to separate the Bluetooth path from librespot and the network.

## Baseline (15:02:52)

```
health : audio_ready true, ble_connected true, speaker_state on
config : "audio_sink_address": "50:1B:6A:14:FD:1D"
bond   : Paired yes, Bonded yes, Trusted yes, Connected yes
links  : ACL 50:1B:6A:14:FD:1D + LE 6C:C0:2C:4B:4B:BE (GATT control)
TX     : 44.7 KB/s during playback
```

## 1. Fault injection — stale bond is diagnosed, not silently retried

`sudo bluetoothctl remove 50:1B:6A:14:FD:1D` at **15:03:08**, deliberately
leaving `config.json` pointing at the now-unknown address. Verified immediately
afterwards that the bond was gone and the config was untouched.

AudioService's response:

```
15:03:15  WARNING A2DP connect failed for 50:1B:6A:14:FD:1D:
                  err:STALE_BOND:device unknown to BlueZ (re-pair required)
15:03:20  WARNING (same, retry in 10s)
15:03:31  WARNING (same, retry in 20s)
15:04:32  WARNING A2DP: 5 consecutive outright connect failures to
                  50:1B:6A:14:FD:1D — cooling down 300s instead of retrying immediately
```

`audio_ready` went `false`; the Spotify grace period started
(`audio unavailable — 300s grace`) rather than deregistering immediately.

The failure is **explicitly classified** (`STALE_BOND`) with an actionable
remedy in the message, and the retry ladder backs off (10 s → 20 s → … → 5
failures → 300 s cooldown) instead of hammering. This is exactly what FAULT-05
requires — "must fail with a clear diagnostic (not silent retry-forever)".

Incidental observation: the BLE GATT control link was unaffected in kind — it
dropped with the device object but reconnected on a fresh RPA and resumed
reading firmware and battery normally. BR/EDR bond removal does not take BLE
control down with it.

**PASS.**

## 2. Recovery — `POST /audio/pair` creates a fresh bond

Operator pressed the speaker's Bluetooth button (LEDs flashing) as the scan was
triggered at **15:04:26**.

```
15:04:26  POST /api/v1/audio/pair -> 202
15:04:26  Pairing: discovering speaker (60s window)
15:04:26  Pairing: speaker BR/EDR address is 50:1B:6A:14:FD:1D (from FDDF)   <1 s
15:04:34  Pairing: 50:1B:6A:14:FD:1D visible on BR/EDR — pairing immediately
15:04:37  Pairing: address 50:1B:6A:14:FD:1D saved to config
15:04:37  Pairing: complete
15:04:37  A2DP: re-pair detected — retrying immediately
15:04:37  audio gate: audio restored within grace period
```

**11 seconds** from API call to audio restored. FDDF address derivation was
effectively instant; the BR/EDR device object appeared ~8 s after the trigger,
i.e. right after the button press — consistent with RC13's BOOT-02 finding that
the JBL answers BR/EDR inquiry only while in pairing mode.

Notable interaction, and the reason this scenario is worth running on hardware:
AudioService had entered its **300 s cooldown** one second before pairing
succeeded. The `re-pair detected — retrying immediately` path broke that
cooldown on the pairing event. Without it, a successful re-pair would have been
followed by up to five minutes of silence with everything reporting healthy —
a bug that no unit test would surface, since it only exists in the timing
overlap between two independent state machines.

**PASS.**

## 3. Post-recovery state — the bond is genuinely fresh

```
bond   : Paired yes, Bonded yes, Trusted yes, Connected yes
paired : Device 50:1B:6A:14:FD:1D JBL PartyBox 520
config : "audio_sink_address": "50:1B:6A:14:FD:1D"   (persisted)
health : audio_ready true
spotify: running true
TX     : 39.7 KB/s, then 44.7 KB/s — matching the pre-fault baseline exactly
```

The bond was rebuilt from nothing: it had been removed from
`/var/lib/bluetooth` at 15:03:08 and re-created at 15:04:37. Config retained
the address across the whole cycle, and `AudioService.update_address()` woke
the service without a restart.

**PASS.**

## Observation: audible A2DP disruption during the BLE reconnect window

Immediately after a successful re-pair, the operator reported ~45 s of heavy
stuttering. This correlates exactly with the BLE control link reconnecting:

```
15:04:33  connection lost, will reconnect (56:3A:44:83:A7:3D)
15:05:01  connection failed (attempt 4): could not connect to 61:F9:04:93:8D:15
15:05:06  scan attempt 5 / scanning for speaker
15:05:19  connected to 68:04:A5:67:4C:8F (attempt 5)
```

BLE scanning and A2DP share one radio, so the scan windows steal air time from
the stream. Throughput measured immediately after the link settled was 39.7
then 44.7 KB/s — full health — and the operator independently confirmed the
stuttering had stopped, so this is a bounded, self-resolving window rather than
a persistent fault.

Recorded as an observation, not a defect: it is inherent to a single-radio
appliance holding both a BLE control link and an A2DP stream to the same
device, and it self-heals. Worth knowing because it makes the minute after any
re-pair a misleading time to judge audio quality — and worth revisiting if the
RPA-rotation reconnect ever takes materially longer than the ~45 s seen here.

## Defects found during this run

Both filed separately with reproduction and evidence, per #77's acceptance
criteria ("filed separately with the captured evidence … not silently
patched without reproduction").

1. **[#98](https://github.com/jklingberg/partybox-companion/issues/98) — a
   failed `Pair()` starves A2DP to ~16% until the Bluetooth stack is
   restarted** (`v1.0 blocker`). `BluezClient.pair()` times out the local
   asyncio task but never aborts the D-Bus call, so `bluetoothd` keeps the
   pairing attempt alive, holding a pending LE connect to the identity address.
   Measured 44.7 → **7.3** → 30.1 KB/s across inject/recover, reproduced 2/2.
   `Device1.CancelPairing()` clears it without a stack restart. This was found
   because an early, seemingly harmless `POST /audio/pair` against an
   already-bonded speaker degraded audio for ~20 minutes before anyone
   connected the two events.

2. **[#99](https://github.com/jklingberg/partybox-companion/issues/99) — the
   librespot `--onevent` shim is written to `/run`, which is mounted
   `noexec`**, so playback events never fire and `/api/v1/spotify` reports
   `stopped` while music is demonstrably playing. Confirmed by direct exec test
   as the `companion` user. Unit tests miss it because `runtime_dir` defaults
   to `/tmp/companion`, which permits exec.

Neither defect is in the FAULT-05 path itself; both were surfaced by exercising
it on real hardware.

## Outcome

**FAULT-05 passes on real hardware.** A stale bond is diagnosed with a
classified, actionable error and a bounded backoff rather than a silent retry
loop; `POST /api/v1/audio/pair` recovers it into a genuinely fresh bond in 11
seconds, with config persistence and no service restart. This closes the last
physical-action scenario outstanding from RC13 and, with BOOT-02 already passed
on 2026-07-03, completes the pairing-path hardware coverage tracked by #77.

Two independent production defects (#98, #99) were found in the process, both
of the same shape as the `XDG_RUNTIME_DIR` defect from the
[ARCH-04 run](2026-07-23-arch04-volume-actuator-hw-validation.md): code that
passes its full unit suite while being broken or harmful in production, because
the appliance environment differs from the test environment in a way nothing
asserted on. #98 is a `v1.0 blocker` and should be fixed before tagging.

The appliance was left bonded, connected, streaming, and healthy.
