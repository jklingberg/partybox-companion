# Research Workspace

This directory is the local workspace for raw reverse engineering artifacts.

**It is excluded from version control.** Do not commit raw files here.

## What belongs here

| Subdirectory | Contents |
|---|---|
| `apk/` | JBL app APK files |
| `jadx/` | JADX projects (decompiled Java) |
| `jadx-export/` | Exported Java source from JADX |
| `btsnoop/` | Android HCI snoop logs (nRF Connect export or adb bugreport) |
| `btmon/` | btmon captures on Linux / Raspberry Pi |
| `logs/` | Raw log output from bleak, adb logcat, and experiment sessions |
| `scripts/` | Throwaway bleak exploration scripts |

## What does NOT belong here

Anything you want to preserve long-term belongs in `docs/reverse-engineering/` instead:

- Protocol findings → `docs/reverse-engineering/protocol.md`
- Confirmed discoveries → `docs/reverse-engineering/discoveries.md`
- Open questions → `docs/reverse-engineering/open-questions.md`

## Capturing Bluetooth traffic

Use **nRF Connect for Android** to export the Android HCI snoop log:

1. Enable Bluetooth HCI snoop log in Android Developer Options
2. Connect the JBL app to the speaker and trigger the action you want to capture
3. In nRF Connect → hamburger menu → Export nRF Connect logs
4. Copy the exported log to `research/btsnoop/`

See `research/btsnoop/README.md` for the full workflow including the `adb bugreport` alternative.
On Linux or the Raspberry Pi, use btmon instead — see `research/btmon/README.md`.

## Scripted exploration

Use **bleak** on macOS for interactive exploration and verification:

```bash
pip install bleak
# write scripts in research/scripts/ — excluded from VCS
```

See `docs/reverse-engineering/guide.md` for the full workflow.
