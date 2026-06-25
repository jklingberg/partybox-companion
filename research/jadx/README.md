# research/jadx/

JADX project files for the JBL app APK. JADX is the starting point for all protocol work — find the command structure in source before capturing traffic.

## Setup

Download JADX from https://github.com/skylot/jadx/releases. The GUI (`jadx-gui`) is easier for initial exploration; the CLI (`jadx`) is better for exporting to `research/jadx-export/` for grep-based analysis.

```bash
# macOS via Homebrew
brew install jadx
```

## Opening the APK

```bash
# Open the GUI
jadx-gui research/apk/partybox-6.x.x.apk
```

In JADX GUI:
- **File → Save project** to save the `.jadx` project file into this directory
- The project file stores your bookmarks, comments, and rename mappings — worth keeping as you accumulate notes

## Key package: com.harman.sdk

The Bluetooth protocol lives inside `com.harman.sdk`. Everything outside this package is UI code, analytics, or cloud services — ignore it.

Subpackages found during initial exploration:

| Package | Contents |
|---|---|
| `com.harman.sdk.command` | Individual command classes (one class per opcode) |
| `com.harman.sdk.impl` | Core implementation classes |
| `com.harman.sdk.impl.connect` | Connection management |
| `com.harman.sdk.impl.setting` | Device setting/configuration commands |

## Command classes

Commands follow a naming convention: `Req` prefix for requests sent to the speaker, `Resp` or `Rsp` for responses. Both directions matter — the response tells you what the speaker echoes back or what it pushes unsolicited.

Classes found during the initial session that are worth understanding first:

- `ReqPowerOnCommand` — power on request; contains the opcode constant
- `ReqPowerOffCommand` — power off request
- `BaseCommand` — abstract base; defines the serialisation contract (opcode, payload, checksum)

Start at `BaseCommand` to understand the wire format, then look at `ReqPowerOnCommand` as a concrete example.

## Connection classes

- `DeviceConnectImpl` — manages RFCOMM connection lifecycle
- `GattControllerImpl` — BLE-related; explore this to understand whether any portion of the protocol uses BLE rather than Classic RFCOMM

> **Note:** The presence of `GattControllerImpl`, `BluetoothGattCharacteristic`, and `writeCharacteristic` references in the codebase raises the question of whether the app uses BLE for some commands and RFCOMM for others. This had not been fully resolved during the initial session. See `docs/reverse-engineering/open-questions.md`.

## Configuration classes

- `AppConfig` — device constants, UUIDs, and feature flags

## JADX project files

Files in this directory (`.jadx` project files) are excluded from version control. Export sources to `research/jadx-export/` for grep-based analysis.
