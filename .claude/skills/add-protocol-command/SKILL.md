---
name: add-protocol-command
description: Steps for adding a new BLE GATT protocol command to the partybox SDK — locating the opcode in the JADX export, validating with a Bluetooth capture, documenting the wire format, and adding the message dataclass/parser/serializer/test. Use when reverse-engineering or wiring up a new PartyBox BLE command.
---

## Protocol work

When adding a new protocol command:
1. Locate opcode in JADX export of the JBL APK (`research/jadx-export/`) — see `docs/reverse-engineering/guide.md`
2. Validate with Bluetooth capture (`research/btsnoop/`)
3. Document in `docs/reverse-engineering/protocol.md`
4. Add message dataclass → update parser/serializer/constants → expose via capability
5. Add fixture-based unit test using real capture bytes

Document observations (what bytes appear on the wire). Do not transcribe or paraphrase proprietary source. Never commit APK files, JADX exports, or decompiled source — `research/` is gitignored for this reason.
