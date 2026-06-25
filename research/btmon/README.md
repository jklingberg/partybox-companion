# research/btmon/

Packet captures from `btmon`, the Linux HCI monitor. `btmon` is the Linux equivalent of Android's HCI snoop log — it captures all Bluetooth traffic at the HCI layer in real time.

> **Note:** `btmon` was not part of the initial reverse engineering session, which used an Android device for captures (see `research/btsnoop/`). This directory exists for Linux-based contributors and for captures taken directly on the Raspberry Pi target hardware.

## Why btmon

When the development machine is Linux (or when you are working directly on the target Raspberry Pi), btmon is more convenient than the Android HCI snoop workflow:

- No Android device required
- Captures happen on the machine running bleak, so stimulus and capture are in the same place
- Real-time output makes it easy to correlate what you sent with what the speaker returned

## Typical workflow

Captures verify discoveries from JADX, not replace them. Always know what you expect to see before you capture it.

```bash
# Terminal 1 — start capture (save to file)
sudo btmon -w research/btmon/$(date +%Y%m%d-%H%M%S)-feature-name.btsnoop

# Terminal 2 — trigger the action (bleak script or manual JBL app)
uv run python research/scripts/send_power_on.py
```

`btmon` saves files in btsnoop format. To inspect captured files:

```bash
# Print decoded frames to stdout
btmon -r research/btmon/20250101-120000-power-on.btsnoop

# Open in Wireshark (optional, for visual frame tree)
wireshark research/btmon/20250101-120000-power-on.btsnoop
```

## What to look for

Filter for RFCOMM frames carrying the application payload. In the btmon output, look for lines mentioning `RFCOMM` or `DLC` — those contain the raw bytes exchanged with the speaker.

Cross-reference the bytes you see with:
- The opcode constants in `com.harman.sdk.command` (from JADX)
- The packet format documented in `docs/reverse-engineering/protocol.md`

## File naming

Use the convention `YYYYMMDD-HHMMSS-description.btsnoop` so captures sort chronologically and the description explains what stimulus was applied.

## Files in this directory

Files here are excluded from version control.
