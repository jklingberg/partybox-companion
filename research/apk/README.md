# research/apk/

APK files from the JBL app (`com.jbl.partybox`). The APK is the primary protocol specification for this project — the decompiled source reveals opcodes, command structures, and capability flags that would take weeks to discover from captures alone.

## Acquiring the APK

The cleanest way to pull the APK from an Android device that already has the JBL app installed:

```bash
# Find the APK path on the device
adb shell pm path com.jbl.partybox
# Example output: package:/data/app/~~abc123==/com.jbl.partybox-xyz==/base.apk

# Pull it to this directory
adb pull /data/app/~~abc123==/com.jbl.partybox-xyz==/base.apk base.apk
```

Rename the output to include the app version number so you can track which version was analysed:

```bash
adb shell dumpsys package com.jbl.partybox | grep versionName
# Example output: versionName=6.x.x

mv base.apk partybox-6.x.x.apk
```

## What to look for

The APK often ships as a split APK (base + splits). The `base.apk` contains the Java classes that matter for the Bluetooth protocol. If the package manager returns multiple paths, pull all of them — the protocol code is in `base.apk`.

## Version matters

Different app versions may add or remove commands. When you decompile a new version:

1. Check whether class names or method signatures in `com.harman.sdk` have changed
2. Compare the command opcode list against `docs/reverse-engineering/protocol.md`
3. Update `docs/reverse-engineering/discoveries.md` if new behaviour is found

## Files in this directory

Files here are excluded from version control. Do not commit APK files. If you find a version-specific discovery worth recording, note it in `docs/reverse-engineering/discoveries.md` with the app version.
