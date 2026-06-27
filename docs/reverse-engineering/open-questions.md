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
- Can multiple control (GATT) connections be held simultaneously?

## Features

- Is there a way to query supported capabilities from the device rather than probing?
- The GATT table exposes several other vendor services (e.g. `00001100-d102-…`, `0000eb10-d102-…`, `0000fe2c-…`) alongside the excelpoint control service. Which carry battery, device info, lights, etc.?
- Are Auracast group commands sent over the same control characteristic?
