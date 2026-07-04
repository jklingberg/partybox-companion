#!/usr/bin/env python3
"""Read device information from a PartyBox.

NOTE: DeviceInfoCapability is not yet implemented — the PartyBox 520 does not
expose the standard BLE Device Information Service. The vendor protocol opcode
for device info is not yet confirmed. See open-questions.md.

    uv run python examples/device_info.py
"""

import asyncio
from collections.abc import Awaitable, Callable

from partybox import Scanner


async def _show(label: str, getter: Callable[[], Awaitable[str]]) -> None:
    """Print one attribute, reporting per-field so one failure doesn't hide the rest."""
    try:
        print(f"{label:<13}: {await getter()}")
    except NotImplementedError as exc:
        print(f"{label:<13}: not yet implemented ({exc})")
    except (TimeoutError, OSError) as exc:
        print(f"{label:<13}: unavailable ({exc})")


async def main() -> None:
    speaker = await Scanner.find(timeout=8.0)
    if speaker is None:
        print("No PartyBox found. Is it powered on and in range?")
        return

    async with speaker:
        info = speaker.device_info
        await _show("Manufacturer", info.manufacturer)
        await _show("Model", info.model)
        await _show("Firmware", info.firmware_version)
        await _show("Serial", info.serial_number)


if __name__ == "__main__":
    asyncio.run(main())
