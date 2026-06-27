#!/usr/bin/env python3
"""Read the battery level from a portable PartyBox.

Battery is only available on models with an internal battery (e.g. PartyBox
110, 310). The PartyBox 520 is mains-powered and will print a "not supported"
message.

    uv run python examples/battery.py
"""

import asyncio

from partybox import Scanner


async def main() -> None:
    speaker = await Scanner.find(timeout=8.0)
    if speaker is None:
        print("No PartyBox found. Is it powered on and in range?")
        return

    async with speaker:
        if speaker.battery is None:
            print("Battery not supported on this model (mains-powered).")
        else:
            level = await speaker.battery.level()
            print(f"Battery: {level}%")


if __name__ == "__main__":
    asyncio.run(main())
