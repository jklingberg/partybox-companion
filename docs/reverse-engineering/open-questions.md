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
- Is there a keep-alive mechanism?
- Can multiple RFCOMM connections be held simultaneously?

## Features

- Is there a way to query supported capabilities from the device rather than probing?
- Are Auracast group commands sent over the same RFCOMM channel?
