"""DeviceManager — owns the lifecycle of a single PartyBox connection.

The manager runs as a long-lived asyncio task. It scans for the speaker,
connects, queries initial state, then holds the connection open. When the
connection is lost it reconnects automatically. On cancellation it
disconnects cleanly.

The daemon owns state. The SDK provides operations. The manager is the
boundary between the two.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from partybox import ConnectionFailedError, ConnectionLostError, PartyBoxDevice, Scanner
from partybox.bluetooth.transport import NotConnectedError

from partyboxd.config import SpeakerSettings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StatusSnapshot:
    """Point-in-time view of the daemon's known device state.

    Fields that cannot yet be determined are ``None``. The daemon fills in
    values as they are confirmed from the device; it never fabricates them.
    """

    connected: bool
    address: str | None
    firmware: str | None
    battery: int | None


_DISCONNECTED = StatusSnapshot(
    connected=False,
    address=None,
    firmware=None,
    battery=None,
)


class DeviceManager:
    """Owns the connection to one PartyBox and maintains current state.

    Instantiate and then call :meth:`run` as an asyncio task::

        manager = DeviceManager(settings.speaker)
        task = asyncio.create_task(manager.run())
        # ... later ...
        task.cancel()
        await task

    While :meth:`run` is active, :meth:`snapshot` returns the current state.
    """

    def __init__(self, settings: SpeakerSettings) -> None:
        self._settings = settings
        self._snapshot: StatusSnapshot = _DISCONNECTED
        self._device: PartyBoxDevice | None = None

    @property
    def snapshot(self) -> StatusSnapshot:
        """Current point-in-time device state. Never blocks."""
        return self._snapshot

    async def run(self) -> None:
        """Main connection loop. Runs until cancelled.

        Scans for the speaker, connects, queries initial state, maintains the
        connection, and reconnects automatically after unexpected drops.
        Cancellation triggers a clean disconnect.
        """
        log.info("device manager started")
        attempt = 0
        try:
            while True:
                attempt += 1
                await self._connect_and_maintain(attempt)
        except asyncio.CancelledError:
            log.info("device manager stopping")
            await self._disconnect()
            raise

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _connect_and_maintain(self, attempt: int) -> None:
        """One pass of the connect → maintain → detect-drop cycle."""
        log.info("scan attempt %d", attempt)
        device = await self._scan()
        if device is None:
            await asyncio.sleep(self._settings.reconnect_delay)
            return

        try:
            await device.connect()
        except ConnectionFailedError as exc:
            log.warning("connection failed (attempt %d): %s", attempt, exc)
            await asyncio.sleep(self._settings.reconnect_delay)
            return

        self._device = device
        log.info("connected to %s (attempt %d)", device.address, attempt)

        await self._refresh(device)

        try:
            await device.drain_until_disconnect()
        except ConnectionLostError:
            log.warning("connection lost, will reconnect (%s)", device.address)
        except NotConnectedError:
            # Clean disconnect — only happens if something calls disconnect()
            # outside the manager (unexpected in normal operation).
            log.info("disconnected from %s", device.address)
        finally:
            self._device = None
            self._snapshot = _DISCONNECTED

    async def _scan(self) -> PartyBoxDevice | None:
        log.info("scanning for speaker")
        try:
            return await Scanner.find(timeout=self._settings.scan_timeout)
        except Exception as exc:
            log.warning("scan failed: %s", exc)
            return None

    async def _refresh(self, device: PartyBoxDevice) -> None:
        """Query initial device state and update the snapshot."""
        firmware: str | None = None
        battery: int | None = None

        try:
            firmware = await device.device_info.firmware_version()
            log.info("firmware version: %s", firmware)
        except Exception as exc:
            log.warning("could not read firmware version: %s", exc)

        try:
            if device.battery is not None:
                battery = await device.battery.level()
                log.info("battery level: %d%%", battery)
        except Exception as exc:
            log.warning("could not read battery level: %s", exc)

        self._snapshot = StatusSnapshot(
            connected=True,
            address=device.address,
            firmware=firmware,
            battery=battery,
        )

    async def _disconnect(self) -> None:
        if self._device is not None:
            try:
                await self._device.disconnect()
            except Exception as exc:
                log.warning("error during disconnect: %s", exc)
            finally:
                self._device = None
                self._snapshot = _DISCONNECTED
