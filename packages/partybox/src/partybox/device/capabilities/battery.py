"""Battery capability — reads battery status via the vendor protocol.

The PartyBox 520 has an internal battery but does **not** expose the standard
BLE Battery Service (``0x180F``). Status is read with the vendor control
protocol: opcode ``0x9D`` (request) elicits a ``0x9E`` TLV response. Confirmed
on hardware 2026-07-05 (both battery and mains). See
docs/reverse-engineering/open-questions.md.

The speaker reports no direct charge percentage; :meth:`level` derives it from
the reported capacities. :meth:`status` exposes the full reading (charging
source, health, capacities, durations).
"""

from __future__ import annotations

import asyncio

from partybox.bluetooth.transport import ControlTransport
from partybox.protocol.codec import decode, encode
from partybox.protocol.messages import BatteryStatusRequest, BatteryStatusResponse

_BATTERY_REQUEST = encode(BatteryStatusRequest())
_RESPONSE_TIMEOUT = 3.0


class BatteryCapability:
    """Reads battery status from a PartyBox with an internal battery."""

    def __init__(self, transport: ControlTransport) -> None:
        self._transport = transport

    async def status(self, *, timeout: float = _RESPONSE_TIMEOUT) -> BatteryStatusResponse:
        """Return the speaker's full battery status.

        Sends ``AA 9D …`` and waits for the ``0x9E`` response, skipping any
        unrelated notifications that arrive first.

        Args:
            timeout: seconds to wait for the response before giving up.

        Raises:
            TimeoutError: if no battery response arrives in time.
            NotConnectedError / ConnectionLostError: on transport failure.
        """
        await self._transport.write(_BATTERY_REQUEST)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for battery status response")
            try:
                raw = await asyncio.wait_for(self._transport.receive(), timeout=remaining)
            except TimeoutError:
                raise TimeoutError("timed out waiting for battery status response") from None
            response = decode(raw)
            if isinstance(response, BatteryStatusResponse):
                return response

    async def level(self) -> int:
        """Return the battery charge level as a percentage (0-100).

        Derived from ``remaining_capacity / full_charge_capacity`` — the speaker
        reports no direct percentage.

        Raises:
            RuntimeError: if the reading lacks the capacities needed to derive a
                percentage.
        """
        status = await self.status()
        percent = status.charge_percent
        if percent is None:
            raise RuntimeError("battery response did not include capacity to derive level")
        return percent
