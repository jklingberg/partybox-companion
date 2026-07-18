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

### Battery status (opcodes 0x9D / 0x9E)

Confirmed on a PartyBox 520 (2026-07-05), captured on both battery and mains power. The 520 **has an internal battery** but exposes neither the standard BLE Battery Service (`0x180F`) nor Device Information Service (`0x180A`); battery status comes only through this vendor command.

| Direction | Bytes | Meaning |
|---|---|---|
| Request (TX) | `AA 9D <n> <feature-id…>` | Request battery status; payload is a list of one-byte feature ids |
| Response (RX) | `AA 9E <len> <TLV…>` | Repeating `[feature-id:1][len:1][value:len]` |

Requesting all features (`AA 9D 0C 01 02 03 04 05 06 07 08 09 0A 0B 0C`) yields, on battery:

```
aa 9e 3f 01 10 48 50 30 30 30 36 2d 43 50 30 30 34 31 32 34 32
      02 02 ca 02 03 02 00 00 04 02 c8 10 05 02 b2 12 06 02 5c 12
      07 02 01 00 08 01 63 09 01 02 0a 01 00 0b 04 e0 0d 00 00 0c 04 cc 06 00 00
```

Numeric values are **little-endian**; `BATTERY_ID` (id 1) is ASCII, not a number.

| id | Feature | Example | Meaning |
|---|---|---|---|
| 1 | BATTERY_ID | `"HP0006-CP0041242"` | ASCII battery part/serial string |
| 2 | REMAINING_PLAYTIME | `ca 02` = 714 | minutes; `0xFFFF` sentinel when on mains |
| 3 | TEMPERATURE_MAX | `00 00` = 0 | — |
| 4 | REMAINING_CAPACITY | `c8 10` = 4296 | mAh |
| 5 | FULL_CHARGE_CAPACITY | `b2 12` = 4786 | mAh |
| 6 | DESIGN_CAPACITY | `5c 12` = 4700 | mAh |
| 7 | CYCLE_COUNT | `01 00` = 1 | charge cycles |
| 8 | STATE_OF_HEALTH | `63` = 99 | percent |
| 9 | CHARGING_STATUS | `02` | `1` = charging (mains), `2` = discharging (battery), `3` = full (mains) |
| 10 | BATTERY_HEALTH_NOTIFICATION | `00` = 0 | — |
| 11 | TOTAL_POWER_ON_DURATION | `e0 0d 00 00` = 3552 | minutes |
| 12 | TOTAL_PLAYBACK_TIME_DURATION | `cc 06 00 00` = 1740 | minutes |

**Charge percentage is derived**, not reported directly: `REMAINING_CAPACITY / FULL_CHARGE_CAPACITY` (battery capture 4296/4786 ≈ 90 %; mains 4349/4786 ≈ 91 %; full 4755/4755 = 100 %). `CHARGING_STATUS` distinguishes power source: values `1` and `3` are mains, `2` is battery.

**The `0x9E` response spans multiple notifications.** The full reading declares a `0x3f` (63-byte) payload — larger than a small ATT MTU — so the speaker splits it across several BLE notifications; only the first carries the `SOF/opcode/length` header, the rest are raw payload continuation. Decoding a lone first fragment silently truncates the frame and drops the late TLV fields, notably `CHARGING_STATUS` (id 9) near the end — observed on the appliance as `100% (unknown source)` when the MTU was small, `100% (full)` when the whole frame fit one notification. The SDK reassembles fragments by the declared length (`FrameReassembler` in `codec.py`) before decoding.

Implemented in `BatteryCapability` (SDK reads via `0x9D`/`0x9E`; detection probes at connect since no service advertises the battery). Surfaced through `/api/v1/battery` (level, power source, charging, health, cycles) and the Companion Portal. Codec fixtures use the real captures — see `test_codec.py`. A second, older command (request sub-code `0x13`, response `0x12` — the power-off state-dump opcode) also carries battery data; `0x9D`/`0x9E` is the cleaner dedicated path. Probe/exploration script: `research/scripts/probe_battery.py`.

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

### No remote "enter Bluetooth pairing mode" command exists in the JBL app (static analysis)

Investigated 2026-07-04 by decompiling the official JBL PartyBox Android control
app (`com.jbl.partybox`). This is the correct app for the 520: its vendor opcodes
match this project's confirmed protocol (e.g. power-on is built as opcode `0x03`
with payload `0x05` → `AA 03 01 05`; firmware, set-name, etc. line up).

Observations:

- The app's complete BLE command surface (`com.harman.sdk.command`, ~70 command
  classes) covers power on/off, EQ/bass/DJ effects, karaoke/mic, Auracast &
  LE-Audio grouping, channel/stereo, lightshow, set-device-name, battery,
  player-info, disconnect, and set-phone-MAC. **There is no command to enter
  pairing mode, make the speaker discoverable, or select the Bluetooth input
  source.**
- The app's "pairing" screens are **instructional only**. `PairingInstructionsFragment`,
  `ConnectionGuideFragment`, and `ActivateSpeakerBluetoothFragment` display
  how-to text and hand the user off to the phone's system Bluetooth settings
  (`android.settings.BLUETOOTH_SETTINGS`); none issues a device command. The
  symbol `enterPairingMode` is a UI `TextView` label (in `FragmentConnectionGuideBinding`),
  not an opcode — its click navigates to another instruction screen.
- Phone side: the app only performs BLE **central scanning**
  (`BleDiscoveryImpl.doStartBleScan`) to find the speaker's LE advertisement. It
  does not call `BluetoothAdapter` discoverable / `createBond` / `startDiscovery`
  to drive Classic pairing itself.

Conclusion: the app cannot put the speaker into BR/EDR pairing mode remotely — it
relies on the user pressing the **physical pairing button**. This matches the
hardware observation that the speaker answers BR/EDR inquiry *only* while in
pairing mode (not steady-on, not post-power-on). This is static-analysis evidence;
an on-wire HCI capture taken while pressing the physical pairing button would
confirm no accompanying BLE frame (procedure in [guide.md](guide.md)). **No
implementation is proposed**, since no such command was demonstrated on the wire.

### Advertisement carries an explicit standby flag and a dedicated "wake" command class exist — opcodes not recovered (static analysis, 2026-07-18)

Investigated to understand how the JBL app always manages to wake a speaker
that our own BR/EDR pages cannot reach in standby (see [standby gate](028-audio-readiness-model.md)
and the open question in [open-questions.md](open-questions.md#connection)).
Decompiled `com.jbl.partybox` v3.14.1 (versionCode `260511127`, `research/apk/base.apk`)
with JADX 1.5.1.

Confirmed by exact match against this project's own live-capture findings —
`com.harman.sdk.setting.AppConfig`'s default config carries the identical
control-channel UUIDs already documented above (`rxUUID`/`txUUID` =
`...2e636f6d0001`/`...0002`), plus previously-undocumented defaults:
`bleScanWindow=15`, `bleScanInterval=2`, `maxReconnectCount=3`,
`maxDiscoverCount=10`, `deviceDuration=30000` (ms — likely how long a
scanned device stays valid in the app's cache), `mtuBrEdr=512`. Also confirms
this project's `PARTYBOX520_PID_STRING` product-ID guess: `"2120"`
(`com.harman.jbl.cd_biz_comm.utils.DeviceUtils`), alongside the PID strings
for the rest of the PartyBox/PartyLight lineup.

Two findings bear directly on the wake question, though neither reaches an
opcode:

- **`com.harman.sdk.utils.Constants.ManufacturerData`** declares named fields
  for a parsed advertisement payload: `Vendor_ID`, `Product_ID`, `Model_ID`,
  `Role`, `Crc`/`Second_Crc`, `Device_Name`, `Connectable`, `Device_Battery`,
  `BatteryCharging`, `Volume`, `PartyConnect_Mode`, `Mute`, and — the
  interesting two — **`BT_Connection_Info`** and **`Ble_Standby_Info`**.
  `com.harman.jbl.cd_biz_comm.utils.DeviceUtils` repeats the same field set
  under a `KEY_*` naming convention. This confirms the app's own advertising
  parser treats "is the speaker in BLE standby" and "what's currently
  connected to it" as fields extracted directly from a broadcast, not
  something it has to open a connection to ask — architecturally the same
  idea as this project's `beacon_seen`/FDDF-presence signal, just apparently
  richer. Which AD structure carries them (FDDF service data, a
  manufacturer-specific `0xFF` structure under company ID `0x0ecb` also seen
  in `Constants.partyBoxVendorId`, or both) and their byte offsets were not
  recovered — the concrete parser (`ParseProcessorKt` module tag:
  `partylightLib_release`) exists only as a stripped log-tag constant in
  this build; its class body did not survive R8 optimization.
- **`com.harman.jbl.cd_biz_comm.wireless_tech.blecommander.ReqCommandProtocol`**,
  the base interface every BLE command implements, declares
  `isDefibrillation(): Boolean` (default `false`). The name is a strong signal
  that a specific command exists whose purpose is reviving/waking the
  speaker, distinct from the ~70 ordinary commands documented in the
  2026-07-04 investigation above — but no class overriding it, and therefore
  no opcode, survived in this build either.

**This build is far more R8-optimized than whatever build the 2026-07-04
investigation used.** That investigation found ~70 concrete command classes
under `com.harman.sdk.command` and a concrete `BleDiscoveryImpl`; neither
exists anywhere in this decompile (`grep`/`find` for both come up empty).
Every class this pass *did* recover intact is one R8 has a structural reason
to preserve — Kotlin `data class`es (`equals`/`hashCode`/`copy` generation),
interfaces, and reflection-visible constants (`@SerializedName` Gson fields,
top-level `const val`s). Ordinary imperative business logic — the actual
byte-level parser and the command implementations, including whichever one
sets `isDefibrillation = true` — is inlined/renamed beyond text-based
recovery in this specific build. It is unclear whether this reflects an app
update that tightened R8 rules since 2026-07-04, or a difference in method
(that investigation may have used JADX-GUI's interactive bytecode-level
"Find Usages", which resolves through obfuscated names via the call graph —
something a flat-text `grep` over a CLI export cannot do).

**Conclusion:** static analysis narrows the wake question — there is likely a
dedicated wake/"defibrillation" command, and the beacon is designed to expose
standby + connection state directly — but does not answer it. **No
implementation is proposed.** The decisive next step is the same one already
open in [open-questions.md](open-questions.md#connection): an on-wire HCI
capture of the JBL app performing a wake-from-standby, now with concrete
things to look for (a command payload distinct from the ordinary command
set; a `BT_Connection_Info`/`Ble_Standby_Info`-carrying advertisement
structure this project's SDK doesn't currently parse). A live scan dump on
the appliance itself — checking whether a manufacturer-specific AD
structure (company ID `0x0ecb`) accompanies the FDDF service data this
project already reads — is a cheaper, phone-free first step toward the same
answer.

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
