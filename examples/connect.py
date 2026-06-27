#!/usr/bin/env python3
"""Find a PartyBox, connect, and print basic device info.

uv run python examples/connect.py
"""

import asyncio

from partybox import Scanner


async def main() -> None:
    speaker = await Scanner.find(timeout=8.0)
    if speaker is None:
        print("No PartyBox found. Is it powered on and in range?")
        return

    async with speaker:
        print(f"Connected: {speaker.is_connected}")
        try:
            print(f"Model    : {await speaker.device_info.model()}")
            print(f"Firmware : {await speaker.device_info.firmware_version()}")
        except NotImplementedError:
            print("Device info: not yet implemented (vendor opcode TBD)")
        battery = speaker.battery
        if battery is None:
            print("Battery  : not supported")
        else:
            print(f"Battery  : {await battery.level()}%")

    print("Disconnected.")


if __name__ == "__main__":
    asyncio.run(main())
