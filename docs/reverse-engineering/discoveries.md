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

### Vendor protocol frame format

All vendor frames share one format, confirmed from multiple captures on a PartyBox 520 (2026-06-27):

```
AA  [opcode: u8]  [length: u8]  [payload: length bytes]
```

No checksum observed. Request opcodes are sent to TX; notification responses arrive on RX. Unknown or malformed frames return no response.

### Power command and response

Written to TX; confirmed on a PartyBox 520:

| Command | Bytes |
|---|---|
| Power on | `AA 03 01 05` |
| Power off | `AA 03 01 04` |

Response notifications (RX) after each power write:

| Frame | Meaning |
|---|---|
| `AA 00 02 03 00` | ACK: opcode `0x00`, payload = `[echoed_cmd=0x03, status=0x00]` |
| `AA 12 04 00 36 01 [state]` | Power state notification via opcode `0x12` TLV, tag `0x36`: `0x01` = ON, `0x00` = OFF |

### ACK frame format (opcode 0x00)

`AA 00 02 [echoed_cmd_opcode] [status]`

The speaker ACKs every command that is accepted. `status=0x00` = success. Observed after power on/off writes.

### Firmware version request / response (opcodes 0x21 / 0x22)

Confirmed on a PartyBox 520 running firmware 26.2.10 (2026-06-27):

| Direction | Bytes | Meaning |
|---|---|---|
| Request (TX) | `AA 21 00` | Request firmware version (zero-length payload) |
| Response (RX) | `AA 22 04 1a 02 0a 00` | Firmware `26.2.10` (major=0x1a=26, minor=0x02=2, patch=0x0a=10, trailing zero) |

### Opcode 0x12 — TLV state notification

The speaker pushes opcode `0x12` notifications spontaneously on state changes (power on/off) and in response to an `AA 12 00` request. The payload is structured as:

```
[sub-byte: u8]  [TLV sequence: tag u8, length u8, value: length bytes, ...]
```

Sub-byte is always `0x00` in observed captures. Known TLV tags (all confirmed from power-off state dump, 2026-06-27):

| Tag | Length | Example value | Decoded |
|---|---|---|---|
| `0x36` | 1 | `01` / `00` | Power state: `0x01` = ON, `0x00` = OFF |
| `0x37` | 6 | `50 1b 6a 14 fd 1d` | Bluetooth MAC address |
| `0x40` | 16 | `47 47 31 33 38 39 2d 44 50 30 30 32 30 36 37 32` | ASCII string `"GG1389-DP0020672"` (model/serial) |
| `0x41` | 3 | `1a 02 0a` | Firmware version 26.2.10 (duplicate of opcode 0x21 response) |

The large state dump (tags 0x31–0x5b) is only observed during the power-off shutdown sequence. Sending `AA 12 00` triggers a partial state response (tag `0x53`, opcode `0x62`, `0xd2`, `0xe2`) but does **not** trigger the full serial/firmware dump.

### Opcode 0x31 — capability list request

Sending `AA 31 00` returns opcode `0x32` with a TLV listing of supported capability flags. Confirmed on PartyBox 520; contents not yet fully decoded.

### EQ band data (opcode 0xe2)

Sending `AA 12 00` triggers an `AA E2 00 62 [98 bytes]` response containing what appears to be EQ band data: 7 groups of 13 bytes each, with IEEE 754 float32 frequency and Q-factor values (e.g. `0x42fa0000` = 125.0 Hz). Not yet decoded further.

### Full GATT table — PartyBox 520 (unbonded connection)

Enumerated on 2026-06-27 from the Pi via `bleak`, without an LE bond. Services accessible without bonding are marked ✅; others time out or return "Service Discovery has not been performed yet" (a BlueZ error indicating the GATT cache is incomplete, consistent with no LE bond).

| Service UUID | Description | Accessible without bond |
|---|---|---|
| `65786365-6c70-6f69-6e74-2e636f6d0000` | excelpoint.com vendor control | ✅ (TX write, RX notify) |
| `0000eb10-d102-11e1-9b23-00025b00a5a5` | Unknown vendor (Qualcomm?) | Partial (`eb12` read times out) |
| `00001844-0000-1000-8000-00805f9b34fb` | Volume Control Service | ❌ |
| `00001850-0000-1000-8000-00805f9b34fb` | Published Audio Capabilities (PACS / LE Audio) | ❌ |
| `00001800-0000-1000-8000-00805f9b34fb` | Generic Access Profile | ❌ (Device Name `0x2a00` fails) |
| `0000fd92-0000-1000-8000-00805f9b34fb` | Vendor specific | ❌ |
| `0000184d-0000-1000-8000-00805f9b34fb` | Microphone Control | ❌ |
| `0000fe2c-0000-1000-8000-00805f9b34fb` | Google Fast Pair | ❌ |
| `00001801-0000-1000-8000-00805f9b34fb` | Generic Attribute Profile (GATT) | ❌ |
| `00001100-d102-11e1-9b23-00025b00a5a5` | Unknown vendor (Qualcomm?) | ❌ |

**Key absence:** the standard BLE **Device Information Service** (`0x180A`) and **Battery Service** (`0x180F`) are **not present** on the PartyBox 520. Device info and battery must be accessed through the vendor excelpoint control protocol, not standard GATT reads. Opcodes TBD (see `open-questions.md`).

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
