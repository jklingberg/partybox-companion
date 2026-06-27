"""partybox — Python SDK for controlling PartyBox speakers.

Typical usage::

    import asyncio
    from partybox import Scanner

    async def main() -> None:
        speaker = await Scanner.find()
        if speaker is None:
            print("No PartyBox found")
            return
        async with speaker:
            await speaker.power.turn_on()
            print(await speaker.device_info.model())
            if speaker.battery is not None:
                print(await speaker.battery.level())

    asyncio.run(main())
"""

__version__ = "0.1.0-dev"

from partybox.bluetooth.transport import (
    BluetoothError,
    ConnectionFailedError,
    ConnectionLostError,
    NotConnectedError,
)
from partybox.device import (
    BatteryCapability,
    DeviceInfoCapability,
    PartyBoxDevice,
    PowerCapability,
)
from partybox.scanner import Scanner

__all__ = [
    "BatteryCapability",
    "BluetoothError",
    "ConnectionFailedError",
    "ConnectionLostError",
    "DeviceInfoCapability",
    "NotConnectedError",
    "PartyBoxDevice",
    "PowerCapability",
    "Scanner",
    "__version__",
]
