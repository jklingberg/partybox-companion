# Protocol Reference

Reverse-engineered JBL PartyBox Bluetooth protocol.

> **Status:** Active research. Findings are added as they are confirmed.

---

## Transport

The PartyBox communicates over **Bluetooth Classic** using the **Serial Port Profile (SPP)**, which maps to an **RFCOMM** channel. There is no BLE involved in speaker control.

| Parameter | Value |
|---|---|
| Profile | SPP (Serial Port Profile) |
| UUID | `00001101-0000-1000-8000-00805f9b34fb` |
| Protocol | RFCOMM |

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

Commands are sent from the host to the speaker.

| Feature | Opcode | Payload | Response | Notes |
|---|---|---|---|---|

## Events

Events are notifications sent from the speaker to the host.

| Event | Opcode | Payload | Notes |
|---|---|---|---|

## Capture Method

Traffic was captured using **nRF Connect for Android** (HCI snoop log export) and explored interactively using **bleak** on macOS. See [guide.md](guide.md) for the full workflow.

Captured logs are stored in `research/nrfconnect/` locally (excluded from VCS).
