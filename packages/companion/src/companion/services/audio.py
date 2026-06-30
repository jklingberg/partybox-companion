"""Bluetooth A2DP audio sink manager.

Maintains the Bluetooth Classic A2DP connection between the Pi and the
speaker. Without this connection PipeWire has no Bluetooth audio sink and
librespot has nowhere to send audio.

The BLE GATT control connection (managed by DeviceManager) and the A2DP audio
connection are separate Bluetooth subsystems. Both can coexist on the same
controller — confirmed in M3.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from companion.config import AudioSettings

log = logging.getLogger(__name__)

_CHECK_INTERVAL = 30.0  # seconds between health checks when connected
_CONNECT_TIMEOUT = 15.0  # seconds to wait for bluetoothctl connect
_RETRY_BASE = 10.0  # initial retry delay after a failed/lost connection
_RETRY_MAX = 300.0  # back off up to 5 minutes when another device is competing


@dataclass(frozen=True)
class AudioStatus:
    """Point-in-time snapshot of A2DP audio connection state."""

    connected: bool
    address: str | None


class AudioService:
    """Maintains the Bluetooth Classic A2DP connection to the speaker.

    Instantiate and call :meth:`run` as an asyncio task::

        service = AudioService(settings.audio)
        task = asyncio.create_task(service.run())

    If no ``sink_address`` is configured at construction time, :meth:`run`
    waits until :meth:`update_address` is called — typically by
    :class:`~companion.services.pairing.PairingService` after a successful
    first-time pairing.

    Uses exponential backoff when repeated connect attempts fail so that phone
    competition or speaker unavailability does not hammer the Bluetooth
    controller and interfere with BLE scanning.
    """

    def __init__(self, settings: AudioSettings) -> None:
        self._address: str | None = settings.sink_address
        self._connected = False
        self._address_ready = asyncio.Event()
        if self._address is not None:
            self._address_ready.set()

    @property
    def status(self) -> AudioStatus:
        return AudioStatus(connected=self._connected, address=self._address)

    def update_address(self, address: str) -> None:
        """Set the A2DP sink address and wake the connection loop.

        Called by PairingService after a successful first-time pairing.
        Safe to call while :meth:`run` is suspended waiting for an address.
        """
        self._address = address
        self._address_ready.set()

    async def run(self) -> None:
        """Ensure A2DP is connected; reconnect on drop. Runs until cancelled.

        If no address is configured, waits until :meth:`update_address` is
        called rather than returning immediately.  This keeps the Supervisor
        from treating a no-address startup as an unexpected exit.
        """
        if not self._address_ready.is_set():
            log.info("A2DP: no sink address configured; waiting for pairing")
            await self._address_ready.wait()

        log.info("Audio service starting (sink=%s)", self._address)
        retry_delay = _RETRY_BASE
        try:
            while True:
                if not await self._is_connected():
                    self._connected = False
                    log.info(
                        "A2DP sink not connected, connecting to %s (retry in %.0fs)",
                        self._address,
                        retry_delay,
                    )
                    await self._connect()
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, _RETRY_MAX)
                else:
                    if retry_delay > _RETRY_BASE:
                        log.info("A2DP connection stable, backoff reset")
                    self._connected = True
                    retry_delay = _RETRY_BASE
                    await asyncio.sleep(_CHECK_INTERVAL)
        except asyncio.CancelledError:
            self._connected = False
            log.info("Audio service stopping")
            raise

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _is_connected(self) -> bool:
        assert self._address is not None
        try:
            proc = await asyncio.create_subprocess_exec(
                "bluetoothctl",
                "info",
                self._address,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            return b"Connected: yes" in stdout
        except (OSError, TimeoutError):
            return False

    async def _connect(self) -> None:
        assert self._address is not None
        try:
            proc = await asyncio.create_subprocess_exec(
                "bluetoothctl",
                "connect",
                self._address,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=_CONNECT_TIMEOUT)
            log.info("A2DP connection established to %s", self._address)
        except (OSError, TimeoutError) as exc:
            log.warning("A2DP connect failed: %s", exc)
