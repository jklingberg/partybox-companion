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

## Capture Method

Traffic was captured using **nRF Connect for Android** (HCI snoop log export) and explored interactively using **bleak** on macOS. See [guide.md](guide.md) for the full workflow.

Captured logs are stored in `research/nrfconnect/` locally (excluded from VCS).
