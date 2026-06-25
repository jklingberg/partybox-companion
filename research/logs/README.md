# research/logs/

Raw log output from tool sessions — bleak runs, adb logcat, btmon, and any other tooling used during the investigation. These are scratch files; findings go in `docs/reverse-engineering/`.

## What goes here

| Type | Source | Notes |
|---|---|---|
| `bleak-*.log` | stdout/stderr from bleak scripts | Copy terminal output here for later reference |
| `logcat-*.txt` | `adb logcat` output | BT stack debug logs from the Android device |
| `btmon-*.log` | btmon terminal output (text mode) | Alternative to the btsnoop binary format |
| `experiment-*.txt` | Freeform notes from a session | What you tried, what worked, what didn't |

## Capturing Android BT debug logs

The Android Bluetooth stack can be coaxed into producing more verbose output via logcat:

```bash
# Capture all Bluetooth-related logcat output
adb logcat -s bluetooth:V BluetoothAdapter:V bt_btif:V bt_hci:V \
    | tee research/logs/logcat-$(date +%Y%m%d-%H%M%S).txt
```

These logs show state machine transitions in the BT stack that are not visible in raw HCI captures. During early investigation they were useful for confirming that a connection was established and which RFCOMM channel was in use.

## Bleak output

When running bleak scripts, pipe stdout and stderr to a log file alongside the terminal:

```bash
uv run python research/scripts/scan.py 2>&1 | tee research/logs/bleak-scan-$(date +%Y%m%d).log
```

This lets you review the output later without re-running the script.

## File naming

Prefix with the source tool and date: `logcat-20250101.txt`, `bleak-power-on-20250101.log`.

## Files in this directory

Files here are excluded from version control.
