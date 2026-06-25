# research/btsnoop/

Android HCI snoop logs. These are the captures used during the initial reverse engineering session to validate findings from JADX.

## Background

Android can record all Bluetooth HCI traffic to a log file in btsnoop format. The HCI layer captures everything — both Classic Bluetooth (RFCOMM) and BLE (GATT) frames — making it more comprehensive than any single-channel capture tool.

## Two ways to obtain HCI snoop logs

### Method 1: nRF Connect for Android (primary)

This was the method used during the initial session. nRF Connect provides a convenient one-tap export that packages the Android HCI snoop log.

1. On your Android device, go to **Settings → Developer Options** and enable **Bluetooth HCI snoop log**
2. Open the JBL app, connect to the speaker, and trigger the action you want to capture
3. Open **nRF Connect** → hamburger menu (⋮) → **Export nRF Connect logs**
4. Transfer the exported archive to your development machine and extract `btsnoop_hci.log`
5. Place it in this directory with a descriptive name

> **Tip:** Toggle the HCI snoop log off and back on before each session to clear old data. The log file grows unbounded otherwise and becomes hard to analyse.

### Method 2: adb bugreport (found to be more useful)

`adb bugreport` generates a zip archive containing a much wider set of logs: the HCI snoop log, the full Bluetooth stack log (`btsnoop_hci.log`), logcat, and system diagnostics. During the initial investigation, the bugreport method proved more useful than the HCI snoop log alone — the Bluetooth stack log (not just raw HCI packets) often contained readable error messages and state transitions that saved significant analysis time.

```bash
# Generate a bugreport (device must be connected via adb)
adb bugreport research/btsnoop/bugreport-$(date +%Y%m%d).zip

# The btsnoop HCI log is inside the zip at:
# FS/data/misc/bluetooth/logs/btsnoop_hci.log
unzip -p research/btsnoop/bugreport-20250101.zip \
    FS/data/misc/bluetooth/logs/btsnoop_hci.log \
    > research/btsnoop/20250101-bugreport-btsnoop.log

# Bluetooth stack logs (often more informative)
unzip research/btsnoop/bugreport-20250101.zip "FS/data/misc/bluetooth/logs/*" -d /tmp/btlogs/
```

## Analysing captures

The btsnoop format is supported by Wireshark. Open the file in Wireshark and filter on `rfcomm` to isolate the frames carrying the application protocol:

```
rfcomm
```

Alternatively, parse the RFCOMM payload directly — the btsnoop format is documented and straightforward to read programmatically if you want to automate analysis.

## What to look for

Cross-reference captured frames against the command classes in `research/jadx-export/`. If JADX shows that `ReqPowerOnCommand` sets opcode byte `0xXX`, find that byte in the RFCOMM payload of the capture taken while toggling power in the JBL app.

## File naming

Use the convention `YYYYMMDD-description.log` (or `.btsnoop`) so captures sort chronologically.

## Files in this directory

Files here are excluded from version control.
