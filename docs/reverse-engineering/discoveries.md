# Discoveries

Reverse engineering findings organised by confidence level.

---

## Confirmed

Findings reproduced reliably across multiple captures.

### Control transport is BLE GATT, not Classic SPP/RFCOMM

Verified on a JBL PartyBox 520 from the Raspberry Pi on 2026-06-26.

- An SDP browse of the Classic address (`50:1B:6A:14:FD:1D`, CoD `0x242428` = Audio/Video) shows **no SPP / RFCOMM / Serial Port service**. Only A2DP (Audio Sink/Source, L2CAP PSM 25), AVRCP TG/CT (PSM 23), and GATT (Generic Access/Attribute over ATT, PSM 31) are advertised. Classic is used for **audio + media transport controls only**.
- Control happens over **BLE GATT**. The device advertises over LE as `JBL PartyBox 520` on a separate identity address (`48:00:57:62:76:66`) plus a rotating random address (`42:BD:30:DE:65:E1`) — distinct from the Classic address.
- Confirmed identical to the original macOS `bleak`-based capture, and reproduced on Linux/BlueZ via `bleak` (BlueZ D-Bus) — both connect and enumerate the same GATT table.

### Control service and characteristics (vendor: "excelpoint")

The control service UUID base is ASCII `"excelpoint.com"`:

| Role | UUID | Properties |
|---|---|---|
| Service | `65786365-6c70-6f69-6e74-2e636f6d0000` | — |
| TX (host → speaker, commands) | `65786365-6c70-6f69-6e74-2e636f6d0002` | `write` |
| RX (speaker → host, notifications) | `65786365-6c70-6f69-6e74-2e636f6d0001` | `read`, `notify` |

### Power command frame

Written to the TX characteristic (GATT write with response succeeded on the Pi):

| Command | Bytes |
|---|---|
| Power on | `AA 03 01 05` |
| Power off | `AA 03 01 04` |

Frame shape (observed): `AA` header, then three bytes; the last byte distinguishes on (`05`) / off (`04`). Field meanings of bytes 2–3 not yet confirmed. No notification was observed in response to power-on while already on.

---

## Likely

Consistent with captures but not yet fully verified.

> None documented yet.

---

## Speculative

Pattern matches but requires more evidence.

> None documented yet.

---

## How to Contribute

1. Capture traffic (see [guide.md](guide.md))
2. Reproduce the finding at least twice
3. Move it to the appropriate confidence section
4. Reference the capture filename so others can verify
5. Open a PR
