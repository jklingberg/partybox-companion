#!/usr/bin/env python3
"""Discover nearby PartyBox speakers.

uv run python examples/scan.py
"""

import asyncio

from partybox.bluetooth import Scanner


async def main() -> None:
    candidates = await Scanner.discover(timeout=8.0)
    if not candidates:
        print("No PartyBox found. Is it powered on and in range?")
        return
    print(f"Found {len(candidates)} PartyBox speaker(s):")
    for c in candidates:
        print(f"  {c.name}  (rssi={c.rssi})")


if __name__ == "__main__":
    asyncio.run(main())
