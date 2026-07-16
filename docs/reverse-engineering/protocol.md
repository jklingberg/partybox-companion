# Protocol Reference

Reverse-engineered JBL PartyBox Bluetooth protocol.

> **Status:** Active research. Findings are added as they are confirmed.

---

## Transport

> **Corrected 2026-06-26 after hardware verification.** An earlier draft of this
> document stated control used Bluetooth Classic SPP/RFCOMM. That is **wrong** —
> the real PartyBox 520 advertises no SPP/RFCOMM service. See
> [discoveries.md](discoveries.md) for the evidence.

Speaker **control** happens over **BLE GATT**. Bluetooth Classic is used only
for **audio (A2DP)** and **media transport controls (AVRCP)**.

Control commands are written to a vendor GATT characteristic (UUID base is ASCII
`"excelpoint.com"`); the speaker replies via notifications on a companion
characteristic.

| Parameter | Value |
|---|---|
| Transport | BLE GATT (ATT) |
| Control service | `65786365-6c70-6f69-6e74-2e636f6d0000` |
| TX characteristic (write commands) | `65786365-6c70-6f69-6e74-2e636f6d0002` |
| RX characteristic (notify responses) | `65786365-6c70-6f69-6e74-2e636f6d0001` |

The LE control identity address is **distinct** from the Classic (A2DP/AVRCP)
address — discovery must scan LE, not Classic inquiry, to find the control
endpoint.

## Tested Hardware

| Model | Firmware | Status | Notes |
|---|---|---|---|
| JBL PartyBox 520 | TBD | Primary test device | |

## Message Format

> To be documented. See `discoveries.md` for current state of frame analysis.

### Frame Structure

```
[ header ] [ opcode ] [ payload length ] [ payload ] [ checksum ]
```

> Exact byte layout to be confirmed and documented here.

## Commands

Commands are written to the TX characteristic (host → speaker).

| Feature | Bytes | Response | Notes |
|---|---|---|---|
| Power on | `AA 03 01 05` | none observed | Verified on PB 520; write-with-response succeeded |
| Power off | `AA 03 01 04` | TBD | Differs from power-on only in the final byte (`04` vs `05`) |

## Events

Events are notifications sent from the speaker to the host.

| Event | Opcode | Payload | Notes |
|---|---|---|---|

## FDDF Advertisement (Service Data)

The speaker continuously broadcasts LE advertisements carrying service data for
UUID `0xFDDF` (Harman International), from a resolvable random address distinct
from both the control and A2DP addresses. The payload embeds the speaker's
BR/EDR (A2DP) address — this is the canonical discovery mechanism (ADR-027) —
and also carries **live connection state**, observed 2026-07-16 on a PartyBox
520 (host: Pi 5, RC14) by toggling a phone's Bluetooth connection while
capturing with `btmon` during LE scans:

| State (live captures) | Payload |
|---|---|
| Phone connected, phone playing | `20 21 01 d4 54 e2 c7 08 06 58 6b 50 1b 6a 14 fd 1d 00 09 00 00 00 00 00` |
| Phone connected, phone playing, Pi also streaming | `20 21 01 d4 53 e2 c7 0c 06 58 6b 50 1b 6a 14 fd 1d 00 09 00 00 00 00 00` |
| Phone connected, idle | `20 21 01 d4 53 e2 c7 0c 06 58 6b 50 1b 6a 14 fd 1d 00 09 00 00 00 00 00` |
| Phone disconnected | `20 21 01 d4 53 e2 c7 0c 05 58 6b 50 1b 6a 14 fd 1d 00 01 00 00 00 00 00` |

Byte-offset observations (0-indexed; **confirmed** = flipped live in both
directions during the session, *tentative* = single observation or hypothesis):

| Offset | Observed values | Interpretation |
|---|---|---|
| 4 | `58`→`54`→`53`, `d2` | **Battery percent in bits 0-6** (0x58 = 88 … 0x53 = 83; tracked the API's battery reading as it drained). **Bit 7 = charging flag**: observed set (`0xd2` = 82% + charging, matching the API) the moment the speaker ran from mains, clear on battery |
| 7 | `18`, `08`, `0c` | *Tentative:* changes with audio/source state, but did not track any single tested variable cleanly — not usable yet |
| 8 | `04`, `06` ↔ `05` | **Connected-source indicator** — `04` with nothing connected (pairing-mode capture and fresh boot before A2DP), `05` with only the companion, `06` with a phone also connected; confirmed live in both directions |
| 9–10 | `00 00` → `58 6b` | *Tentative:* set when the phone connected but did **not** clear on disconnect — likely a last-connected-device identifier |
| 11–16 | `50 1b 6a 14 fd 1d` | **BR/EDR (A2DP) address**, big-endian (ADR-027) |
| 18 | `09` ↔ `01` | **Connection bitmask** — bit `0x08` set while the phone was connected, cleared on disconnect |

**Why this matters:** A2DP itself gives the source no feedback about rendering.
When a second device (typically a phone) is connected, the speaker can accept
the companion's stream (`MediaTransport1` goes `active`, no AVDTP error) while
rendering silence — every Pi-side signal looks healthy. Offsets 8 and 18 are
the only known observables that reveal "another source is connected", making a
passive FDDF watch the candidate mechanism for an `audio_focus` health signal.
Focus arbitration itself appears unreliable: with a phone actively playing, one
companion stream was silently discarded while an identical retry a minute later
stole focus (phone playback stopped and did not resume).

## Capture Method

Traffic was captured using **nRF Connect for Android** (HCI snoop log export) and explored interactively using **bleak** on macOS. See [guide.md](guide.md) for the full workflow.

Captured logs are stored in `research/nrfconnect/` locally (excluded from VCS).
