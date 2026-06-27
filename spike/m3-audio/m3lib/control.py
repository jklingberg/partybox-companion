"""BLE control-channel probe, built on the ``partybox`` SDK.

This is the *control* half of the coexistence question. It uses the real SDK
(``Scanner`` + ``ControlTransport``) so the spike exercises the same BLE path
the daemon will use, on the same adapter that is simultaneously sourcing A2DP.

The default probe is **non-destructive**: it writes the power-on frame, which is
idempotent when the speaker is already on, and confirms the GATT write succeeds
and the link stays up. A deliberate, opt-in power *cycle* is provided separately
to answer "does power-off/on work while audio is playing" — that one does
interrupt audio, by design.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from partybox.bluetooth import (
    ConnectionLostError,
    ControlTransport,
    NotConnectedError,
    Scanner,
)

# Raw frames; the typed protocol layer arrives in M4 (see examples/power_on.py).
POWER_ON = bytes.fromhex("AA030105")
POWER_OFF = bytes.fromhex("AA030104")


@dataclass
class ProbeResult:
    ok: bool
    latency_ms: float | None
    error: str | None = None


class BleControl:
    """Holds one BLE control connection and probes its liveness.

    A background task drains notifications so the SDK's receive path is active
    (mirroring real use) and so an unexpected drop is observed promptly.
    """

    def __init__(self) -> None:
        self._transport: ControlTransport | None = None
        self._drain: asyncio.Task[None] | None = None
        self.notifications = 0
        self.dropped = False

    @property
    def connected(self) -> bool:
        return self._transport is not None and self._transport.is_connected

    async def connect(self, *, scan_timeout: float = 8.0) -> bool:
        """Discover a PartyBox by name and open a control connection."""
        candidate = await Scanner.find(timeout=scan_timeout)
        if candidate is None:
            return False
        self._transport = await candidate.connect()
        self.dropped = False
        self._drain = asyncio.create_task(self._drain_notifications())
        return True

    async def probe(self, frame: bytes = POWER_ON) -> ProbeResult:
        """Write one frame and time the round-trip to the GATT layer."""
        if self._transport is None:
            return ProbeResult(ok=False, latency_ms=None, error="not connected")
        start = time.monotonic()
        try:
            await self._transport.write(frame)
        except (ConnectionLostError, NotConnectedError) as exc:
            self.dropped = True
            return ProbeResult(ok=False, latency_ms=None, error=f"{type(exc).__name__}: {exc}")
        return ProbeResult(ok=True, latency_ms=round((time.monotonic() - start) * 1000, 1))

    async def power_cycle(self, *, off_for: float = 8.0) -> None:
        """Send power-off, wait, then power-on (interrupts audio deliberately)."""
        await self.probe(POWER_OFF)
        await asyncio.sleep(off_for)
        await self.probe(POWER_ON)

    async def close(self) -> None:
        if self._drain is not None:
            self._drain.cancel()
            with _suppress_cancelled():
                await self._drain
        if self._transport is not None:
            await self._transport.disconnect()
            self._transport = None

    async def _drain_notifications(self) -> None:
        transport = self._transport
        if transport is None:
            return
        try:
            while True:
                await transport.receive()
                self.notifications += 1
        except (ConnectionLostError, NotConnectedError):
            self.dropped = True


class _suppress_cancelled:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> bool:
        return exc_type is not None and issubclass(exc_type, asyncio.CancelledError)
