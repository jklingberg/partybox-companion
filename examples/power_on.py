#!/usr/bin/env python3
"""Power on a PartyBox over the BLE control transport.

    uv run python examples/power_on.py

Note: the power-on command frame lives here as a raw byte literal only because
the protocol layer (M4) does not exist yet. Once it does, this will use a typed
command instead of hex.
"""

import asyncio

from partybox.bluetooth import Scanner

POWER_ON = bytes.fromhex("AA030105")


async def main() -> None:
    speaker = await Scanner.find(timeout=8.0)
    if speaker is None:
        print("No PartyBox found. Is it powered on and in range?")
        return

    print(f"Connecting to {speaker.name} ...")
    transport = await speaker.connect()
    try:
        await transport.write(POWER_ON)
        print(f"Sent power-on ({POWER_ON.hex(' ')}).")
    finally:
        await transport.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
