#!/usr/bin/env python3
"""Start the partyboxd daemon programmatically.

Normally you would run the daemon via its console script::

    partyboxd

This example shows how to instantiate and run it from Python, which is
useful for embedding the daemon in a larger asyncio application or for
integration tests that need a running daemon.

uv run python examples/start_daemon.py
"""

import asyncio

from partyboxd.__main__ import run
from partyboxd.config import ServerSettings, Settings, SpeakerSettings


async def main() -> None:
    settings = Settings(
        speaker=SpeakerSettings(scan_timeout=8.0, reconnect_delay=5.0),
        server=ServerSettings(host="127.0.0.1", port=8765),
    )
    print(f"Starting partyboxd on {settings.server.host}:{settings.server.port}")
    print("Press Ctrl-C to stop.")
    await run(settings)


if __name__ == "__main__":
    asyncio.run(main())
