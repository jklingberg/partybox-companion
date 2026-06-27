#!/usr/bin/env python3
"""Power off a PartyBox.

uv run python examples/power_off.py
"""

import asyncio

from partybox import Scanner


async def main() -> None:
    speaker = await Scanner.find(timeout=8.0)
    if speaker is None:
        print("No PartyBox found. Is it powered on and in range?")
        return

    async with speaker:
        await speaker.power.turn_off()
        print("Powered off.")


if __name__ == "__main__":
    asyncio.run(main())
