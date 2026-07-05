# Open Questions

Research threads and known unknowns. Move entries here when a question is identified; close them out in `discoveries.md` when answered.

---

## Protocol

- What is the exact checksum algorithm?
- Are multi-byte opcodes used, or is opcode always a single byte?
- Is there a session handshake or authentication step?
- What is the maximum payload size?

## Models

- Which capabilities are common across all PartyBox models?
- Do earlier models (e.g. PartyBox 300, 310) use the same frame format?
- Are opcode values consistent across firmware versions?

## Connection

- Does the speaker disconnect clients after a timeout?
- Is there a keep-alive mechanism (does the RX characteristic emit periodic notifications)?
- **BLE address rotation / bonding (M3).** The PartyBox advertises with rapidly-rotating resolvable private addresses — three distinct addresses for one speaker were seen within a single 10 s scan. Connecting by a stale address string drops the link immediately; an unbonded connection is unstable. The fix is to **bond** the speaker once, after which BlueZ resolves any RPA to the stable identity address (`48:00:57:62:76:66` on the test unit) and `BleakTransport(identity_address)` should connect reliably. Open: confirm bonded reconnect is stable across standby cycles; decide where the appliance performs the one-time bond (M3/M7 setup flow). Bonding requires the speaker awake and in pairing mode — it refuses new bonds in standby.
- How is the speaker woken from standby? It stops accepting new BLE bonds in standby; does it still accept a control connection from an already-bonded host to receive the power-on frame, or is a separate wake path needed?
- **Can Bluetooth pairing mode be triggered remotely over BLE? — Likely no (2026-07-04).** Static analysis of the JBL app found no "enter pairing mode / become discoverable / select BT source" command anywhere in its BLE command set; the app's pairing screens are instructional and defer to the phone's system Bluetooth settings. See the [discoveries.md](discoveries.md#likely) finding. This means the appliance-setup step of pressing the physical pairing button cannot (on current evidence) be automated. Open: on-wire HCI confirmation, and whether an already-bonded host reconnecting is a viable substitute for re-pairing.
- **GATT connection exclusivity (confirmed 2026-06-28).** The speaker accepts only one GATT client at a time. When the JBL Portable app is open on a phone, it holds the GATT connection and the Pi's connection attempt either times out or is dropped during service discovery (`BleakError: failed to discover services, device disconnected`). Closing the JBL app releases the connection and the Pi reconnects on the next scan cycle (~30 s). A `hciconfig hci0 reset` on the Pi speeds recovery by clearing any wedged BlueZ state. Root constraint is the speaker's single-client GATT limit; LE bonding (deferred) won't change this. Workaround documented in Portal UI.

## Firmware version opcode regression (2026-06-27)

**Symptom:** `AA 21 00` (firmware version request, previously confirmed opcode) no longer elicits an `AA 22 ...` response. Instead the speaker replies with `AA 12 04 00 53 01 00` (a state-dump notification, opcode `0x12`). This was reproduced across multiple connection attempts.

**Confirmed via probe script (2026-06-27):**
- Connecting without writing anything → no spontaneous notifications
- Sending `AA 21 00` → speaker replies with `AA 12 04 00 53 01 00`; no `AA 22` follows within 5 s

**Payload decode (partial):** `AA 12 04 | 00 53 01 00`. Opcode `0x12`, length 4. Payload meaning unknown.

**Possible explanations (none confirmed):**
- Possible firmware update that changed opcode `0x21` behaviour (unconfirmed — no version comparison available)
- Response may be state-dependent: `0x22` may only be sent in certain operating states (e.g. fully active vs. standby-idle while mains-powered)
- `0x12` may have always been triggered by `0x21`, with `0x22` following quickly; the observed change may be that `0x22` is now absent or the timing has changed

**Impact:** `firmware_version()` raises `TimeoutError`; `DeviceInfoCapability.firmware_version()` is broken on the test unit. The daemon handles this with graceful degradation (`firmware: null` in status). The M4 hardware test `test_device_info_firmware_version` now fails.

**Investigation paths:**
- Capture JBL app traffic to see how it queries firmware in the current state
- Try `AA 21 00` immediately after a power cycle (speaker may respond differently when freshly powered)
- Inspect JADX export for opcode `0x21` usage — check if it changed in newer APK versions

## Features

- Is there a way to query supported capabilities from the device rather than probing?
- **Model / serial number opcode.** The full device identifier string `"GG1389-DP0020672"` appears in the opcode-`0x12` TLV state dump (tag `0x40`) that the speaker pushes during power-off shutdown. No direct request opcode was found: probing `AA 40 00` and nearby opcodes (0x3d–0x45) returned no responses. The string may only be available during a power-state transition. The `DeviceInfoCapability.model()` and `serial_number()` methods raise `NotImplementedError` until the opcode is confirmed. Possible paths: APK analysis in JADX, or capturing traffic from the JBL PartyBox iOS/Android app during a session that shows device info.
- ~~**Battery opcode.**~~ **Resolved (2026-07-05)** — the PartyBox 520 has an internal battery, read via vendor opcodes `0x9D`/`0x9E` (it does not expose the standard `0x180F` Battery Service). Confirmed on hardware, implemented in `BatteryCapability` and `/api/v1/battery`. See [discoveries.md → Battery status (opcodes 0x9D / 0x9E)](discoveries.md#battery-status-opcodes-0x9d--0x9e).
- The `0000eb10-d102-11e1-9b23-00025b00a5a5` vendor service (Qualcomm?) has a readable characteristic (`eb12`) that hangs on GATT read without bonding. Does it expose useful data after bonding?
- Are Auracast group commands sent over the same control characteristic?
