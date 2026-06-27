#!/usr/bin/env python3
"""Find a PartyBox and open a control connection.

uv run python examples/connect.py
"""

import asyncio

from partybox.bluetooth import Scanner


async def main() -> None:
    speaker = await Scanner.find(timeout=8.0)
    if speaker is None:
        print("No PartyBox found. Is it powered on and in range?")
        return

    print(f"Connecting to {speaker.name} ...")
    transport = await speaker.connect()
    try:
        print(f"Connected: {transport.is_connected}")
    finally:
        await transport.disconnect()
    print("Disconnected.")


if __name__ == "__main__":
    asyncio.run(main())
