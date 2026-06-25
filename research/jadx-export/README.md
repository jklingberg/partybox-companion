# research/jadx-export/

Java sources exported from JADX. Exporting makes the entire codebase grep-able, which is faster than navigating the GUI when you know what you're looking for.

## Exporting from JADX

```bash
# Export all sources (slow the first time; subsequent runs are incremental)
jadx --export-gradle research/apk/partybox-6.x.x.apk --output-dir research/jadx-export/

# Or from the JADX GUI: File → Export Gradle project
```

The output is structured as a Gradle project. The Java classes are under:
```
research/jadx-export/sources/src/main/java/
```

## Searching the sources

All useful protocol-related searching uses `grep -r` from the export root. The following searches were useful during the initial session:

```bash
# Find all command classes (naming convention)
grep -r "class Req" sources/ --include="*.java" -l

# Find the command base class
grep -r "class BaseCommand" sources/ --include="*.java" -l

# Find where commands are dispatched
grep -r "sendCommand" sources/ --include="*.java" -l

# Find opcode constants
grep -r "identifier" sources/ --include="*.java" | grep -v "import"

# Find response handling
grep -r "responseCommands" sources/ --include="*.java" -l

# Find connection entry points
grep -r "class DeviceConnectImpl\|class GattControllerImpl" sources/ --include="*.java" -l

# Find BLE GATT usage (to understand whether BLE is used alongside Classic)
grep -r "BluetoothGattCharacteristic\|writeCharacteristic" sources/ --include="*.java" -l

# Find the SPP / RFCOMM UUID
grep -r "00001101" sources/ --include="*.java"

# Find power-related commands
grep -r "PowerOn\|PowerOff\|power_on\|power_off" sources/ --include="*.java" -l
```

## Workflow

The typical session:

1. Grep for a known concept (e.g. `PowerOn`) to find the relevant command class
2. Open the class in JADX GUI to read the full implementation with cross-references
3. Note the opcode byte(s) and payload structure
4. Write a bleak script in `research/scripts/` to send that command
5. Validate with a capture in `research/btsnoop/`

## Files in this directory

Files here are excluded from version control. The export can be large (tens of thousands of files). Re-run the export whenever you pull a new APK version.
