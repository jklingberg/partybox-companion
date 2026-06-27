#!/usr/bin/env python3
"""Read device information from a PartyBox.

NOTE: DeviceInfoCapability is not yet implemented — the PartyBox 520 does not
expose the standard BLE Device Information Service. The vendor protocol opcode
for device info is not yet confirmed. See open-questions.md.

    uv run python examples/device_info.py
"""

import asyncio

from partybox import Scanner


async def main() -> None:
    speaker = await Scanner.find(timeout=8.0)
    if speaker is None:
        print("No PartyBox found. Is it powered on and in range?")
        return

    async with speaker:
        try:
            print(f"Manufacturer : {await speaker.device_info.manufacturer()}")
            print(f"Model        : {await speaker.device_info.model()}")
            print(f"Firmware     : {await speaker.device_info.firmware_version()}")
            print(f"Serial       : {await speaker.device_info.serial_number()}")
        except NotImplementedError as e:
            print(f"Not yet implemented: {e}")


if __name__ == "__main__":
    asyncio.run(main())
