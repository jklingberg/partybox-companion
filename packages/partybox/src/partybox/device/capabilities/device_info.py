"""Device information capability — model, manufacturer, firmware, serial.

Confirmed from hardware (JBL PartyBox 520, 2026-06-27):
- Standard BLE Device Information Service (0x180A) is absent.
- Firmware version: opcode 0x21 request → opcode 0x22 response.
- Serial number and model: only observed in the vendor TLV state dump pushed
  during power-off (opcode 0x12, tag 0x40). No request opcode found yet.

See docs/reverse-engineering/discoveries.md for evidence and raw captures.
"""

from __future__ import annotations

import asyncio

from partybox.bluetooth.transport import ControlTransport
from partybox.protocol.codec import decode, encode
from partybox.protocol.messages import FirmwareVersionRequest, FirmwareVersionResponse

_FIRMWARE_REQUEST = encode(FirmwareVersionRequest())
_RESPONSE_TIMEOUT = 3.0


class DeviceInfoCapability:
    """Reads static device attributes from the speaker."""

    def __init__(self, transport: ControlTransport) -> None:
        self._transport = transport

    async def manufacturer(self) -> str:
        """Return the manufacturer name."""
        return "JBL"

    async def model(self) -> str:
        """Return the model number string (e.g. ``"PartyBox 520"``)."""
        raise NotImplementedError(
            "model opcode not yet confirmed — see docs/reverse-engineering/open-questions.md"
        )

    async def firmware_version(self) -> str:
        """Return the firmware revision string (e.g. ``"26.2.10"``)."""
        await self._transport.write(_FIRMWARE_REQUEST)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _RESPONSE_TIMEOUT
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for firmware version response")
            try:
                raw = await asyncio.wait_for(self._transport.receive(), timeout=remaining)
            except TimeoutError:
                raise TimeoutError("timed out waiting for firmware version response") from None
            response = decode(raw)
            if isinstance(response, FirmwareVersionResponse):
                return str(response)

    async def serial_number(self) -> str:
        """Return the serial number string."""
        raise NotImplementedError(
            "serial number opcode not yet confirmed — "
            "see docs/reverse-engineering/open-questions.md"
        )
