# Protocol Analysis Guide

How to analyse the PartyBox Bluetooth protocol and contribute to the independent implementation.

This is developer documentation for contributors extending the protocol implementation. Before contributing, read the [Legal hygiene](../../CONTRIBUTING.md#legal-hygiene) section of the Contributing guide — particularly the guidance on documenting observations rather than copying proprietary code.

---

## Overview

> **Transport correction (2026-06-26):** Hardware verification showed speaker
> **control runs over BLE GATT**, not Bluetooth Classic RFCOMM — the speaker
> advertises no SPP/RFCOMM service. Classic carries only A2DP audio and AVRCP.
> The RFCOMM/SPP references below are historical; control commands are written
> to a vendor GATT characteristic and responses arrive as notifications. See
> [ADR-015](../adr/015-bluetooth-control-transport.md), [protocol.md](protocol.md),
> and [discoveries.md](discoveries.md). The `bleak`-based interactive approach
> below is the correct one.

The JBL PartyBox uses Bluetooth Classic for audio (A2DP) and BLE GATT for control. The approach used to develop the independent implementation is:

1. Analyse the official Android app structure using **JADX** to understand the protocol's message types and opcodes
2. Capture HCI traffic on Android using **nRF Connect** while triggering actions in the JBL app to validate the analysis
3. Explore and test the protocol interactively using **bleak** on macOS
4. Document confirmed findings in `docs/reverse-engineering/` as protocol observations

---

## Tools

| Tool | Purpose | Platform |
|---|---|---|
| [nRF Connect for Android](https://play.google.com/store/apps/details?id=no.nordicsemi.android.mcp) | Capture HCI snoop logs while the JBL app is running | Android |
| [bleak](https://bleak.readthedocs.io/) | Programmatic Bluetooth exploration and scripting | macOS / Linux |
| JADX | Decompile the JBL APK to cross-reference opcodes | Any |

---

## Capturing Bluetooth traffic with nRF Connect (Android)

nRF Connect can export Android's built-in HCI snoop log, which captures all Bluetooth traffic at the HCI layer — including the RFCOMM frames exchanged between the JBL app and the speaker.

1. On your Android device, enable **Developer Options** (Settings → About Phone → tap Build Number 7 times)
2. In Developer Options, enable **Bluetooth HCI snoop log**
3. Open the JBL app and connect to your PartyBox
4. Trigger the action you want to capture (change volume, toggle power, etc.)
5. Open **nRF Connect** → hamburger menu → **Export nRF Connect logs**
6. The export includes the HCI snoop log — copy it to `research/btsnoop/` on your development machine

The HCI snoop log is in btsnoop format and can be opened with Wireshark if you want a visual view. Alternatively, parse the RFCOMM payload bytes directly.

**Tip:** Clear the HCI log before each capture session so the file stays small and easy to analyse. On most devices: toggle Bluetooth HCI snoop log off and back on.

---

## Exploring with bleak (macOS)

[bleak](https://bleak.readthedocs.io/) is a Python Bluetooth library that works on macOS without requiring root or a Linux kernel. It is useful for scripted exploration and for verifying findings interactively.

```bash
# Install in the research scripts environment
pip install bleak

# Or add a throwaway script to research/scripts/
```

Store exploration scripts in `research/scripts/`. These are excluded from VCS — they are throwaway tooling, not production code. Once a script confirms a finding, document the opcode in `docs/reverse-engineering/protocol.md` and write a proper test fixture, then discard the script.

---

## Analysing captures

### Finding the RFCOMM frames

In the HCI snoop log, look for RFCOMM data frames. The JBL app uses SPP (UUID `00001101-0000-1000-8000-00805f9b34fb`) over RFCOMM.

Look for repeating patterns across different commands:
- A consistent header sequence at the start of each frame
- A length field that varies with payload size
- A trailing byte that changes with content (likely a checksum)

### Cross-referencing with the APK

JADX can decompile the JBL app and reveal opcode constants and message class names. Store the APK in `research/apk/` and the JADX export in `research/jadx-export/`. Both are excluded from VCS.

```bash
grep -r "0x" research/jadx-export/ | grep -i "volume\|power\|eq"
```

---

## Documenting findings

Once you have confirmed a finding:

1. **Add to `protocol.md`** — the opcode, payload layout, and expected response.

2. **Add to `discoveries.md`** — under the appropriate confidence level (Confirmed / Likely / Speculative).

3. **Close the question in `open-questions.md`** if it was listed there.

4. **Write a test fixture** — capture the exact bytes as a Python `bytes` literal. This lets CI verify the codec without hardware.

```python
# Example fixture for a volume command
VOLUME_SET_40 = bytes.fromhex("aa550102000128".replace(" ", ""))
```

---

## Contributing

If you have a PartyBox model not yet documented:

1. Follow the capture workflow above
2. Compare opcodes with the existing entries in `protocol.md`
3. Document what matches, what differs, and what is new
4. Open a PR adding your findings

Cross-model consistency (or the lack of it) is useful data. Don't assume your model matches the existing docs.
