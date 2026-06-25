# research/scripts/

Throwaway bleak scripts used during the initial reverse engineering session and for ongoing protocol exploration. These scripts never became part of the SDK — they are short, disposable experiments.

## Progression during the initial session

The scripts evolved through a predictable pattern:

1. **Scanner** — enumerate nearby Bluetooth devices, confirm the PartyBox is visible and note its MAC address and advertised services
2. **Connector** — establish a Bluetooth Classic RFCOMM connection to the SPP service UUID (`00001101-0000-1000-8000-00805f9b34fb`), verify that the socket stays open
3. **Listener** — connect and read all unsolicited bytes the speaker sends on connect; observe any handshake or hello packet
4. **Command sender** — send a specific byte sequence derived from JADX analysis; observe the speaker's response
5. **Validator** — automate a test sequence (connect → send command → read response → assert) to confirm a command works reliably before documenting it

Each script in this directory represents one of these stages for a specific capability.

## Setup

These scripts depend only on bleak. Do not use the project's uv workspace for them — keep them self-contained so they can be run without setting up the full development environment.

```bash
pip install bleak
```

Or, if you prefer isolation:

```bash
python -m venv .venv-research
.venv-research/bin/pip install bleak
.venv-research/bin/python research/scripts/scan.py
```

## Writing a new experiment script

Keep scripts short and focused. A typical script looks like:

```python
#!/usr/bin/env python3
"""
Send ReqPowerOnCommand and observe response.
Opcode from: com.harman.sdk.command.ReqPowerOnCommand (JADX)
"""
import asyncio
from bleak import BleakScanner, BleakClient

SPP_UUID = "00001101-0000-1000-8000-00805f9b34fb"
TARGET_NAME = "JBL PartyBox 520"

POWER_ON_CMD = bytes([0xAA, ...])  # fill in from JADX

async def main():
    devices = await BleakScanner.discover()
    target = next(d for d in devices if TARGET_NAME in d.name)
    async with BleakClient(target) as client:
        await client.write_gatt_char(SPP_UUID, POWER_ON_CMD)
        response = await client.read_gatt_char(SPP_UUID)
        print(f"Response: {response.hex()}")

asyncio.run(main())
```

## When a script confirms a finding

Move the finding out of the script and into the permanent docs:

1. Add the confirmed opcode and payload format to `docs/reverse-engineering/protocol.md`
2. Add the discovery to `docs/reverse-engineering/discoveries.md` with its confidence level
3. Implement it in `packages/partybox/src/partybox/messages.py`

The script itself does not need to be cleaned up or kept — it served its purpose.

## Files in this directory

Files here are excluded from version control.
