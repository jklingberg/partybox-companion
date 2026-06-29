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
from collections.abc import Callable
from dataclasses import dataclass

from partybox import ConnectionFailedError, ConnectionLostError, PartyBoxDevice, Scanner
from partybox.bluetooth.transport import NotConnectedError

from partyboxd.config import SpeakerSettings
from partyboxd.device.events import (
    ConnectedEvent,
    DeviceEvent,
    DisconnectedEvent,
    EventBus,
    PowerChangedEvent,
)

log = logging.getLogger(__name__)


class DeviceNotConnectedError(Exception):
    """Raised when a device operation is attempted while the speaker is not connected."""


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
    Callers may :meth:`subscribe` to receive device events as they occur.
    """

    def __init__(
        self,
        settings: SpeakerSettings,
        volume_fallback: Callable[[], int | None] | None = None,
    ) -> None:
        self._settings = settings
        self._snapshot: StatusSnapshot = _DISCONNECTED
        self._device: PartyBoxDevice | None = None
        self._bus = EventBus()
        # Optional callable that returns a software-based volume reading when
        # the BLE opcode is not yet available. The companion layer wires this to
        # VolumeState.level; the bare daemon leaves it as None.
        self._volume_fallback = volume_fallback

    @property
    def snapshot(self) -> StatusSnapshot:
        """Current point-in-time device state. Never blocks."""
        return self._snapshot

    def subscribe(self) -> asyncio.Queue[DeviceEvent]:
        """Subscribe to device events. Returns a queue that receives future events.

        Call :meth:`unsubscribe` with the returned queue when done.
        """
        return self._bus.subscribe()

    def unsubscribe(self, queue: asyncio.Queue[DeviceEvent]) -> None:
        """Stop delivering events to *queue*."""
        self._bus.unsubscribe(queue)

    async def get_volume(self) -> int | None:
        """Return the current speaker volume (0-100), or ``None`` if unavailable.

        Resolution order:
        1. BLE hardware read via the SDK.
        2. Software fallback from ``volume_fallback`` (set by companion layer)
           when the BLE opcode is not yet implemented.
        3. ``None`` when neither source has a value.

        Raises :exc:`DeviceNotConnectedError` if the speaker is not connected.
        """
        device = self._device
        if device is None:
            raise DeviceNotConnectedError()
        try:
            return await device.volume.get()
        except NotImplementedError:
            # BLE opcode not yet confirmed — fall back to software state if
            # the companion layer has wired one in.
            if self._volume_fallback is not None:
                return self._volume_fallback()
            return None
        except (ConnectionLostError, NotConnectedError) as exc:
            raise DeviceNotConnectedError() from exc

    async def set_volume(self, percent: int) -> None:
        """Send a volume command to the connected speaker.

        Args:
            percent: the desired volume level, 0-100.

        Raises:
            ValueError: if *percent* is outside 0-100.
            DeviceNotConnectedError: if the speaker is not connected or the
                connection is lost during the command.
            NotImplementedError: if the BLE volume opcode is not yet implemented.
        """
        device = self._device
        if device is None:
            raise DeviceNotConnectedError()
        try:
            await device.volume.set(percent)
        except (ConnectionLostError, NotConnectedError) as exc:
            raise DeviceNotConnectedError() from exc

    async def power_on(self) -> None:
        """Send a power-on command to the connected speaker.

        Raises :exc:`DeviceNotConnectedError` if the speaker is not connected
        or if the connection is lost during the command.
        """
        device = self._device
        if device is None:
            raise DeviceNotConnectedError()
        try:
            await device.power.turn_on()
        except (ConnectionLostError, NotConnectedError) as exc:
            raise DeviceNotConnectedError() from exc
        self._bus.emit(PowerChangedEvent(state="on"))

    async def power_off(self) -> None:
        """Send a power-off command to the connected speaker.

        Raises :exc:`DeviceNotConnectedError` if the speaker is not connected
        or if the connection is lost during the command.
        """
        device = self._device
        if device is None:
            raise DeviceNotConnectedError()
        try:
            await device.power.turn_off()
        except (ConnectionLostError, NotConnectedError) as exc:
            raise DeviceNotConnectedError() from exc
        self._bus.emit(PowerChangedEvent(state="off"))

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
        snap = self._snapshot
        self._bus.emit(
            ConnectedEvent(
                address=snap.address,
                firmware=snap.firmware,
                battery=snap.battery,
            )
        )

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
            self._bus.emit(DisconnectedEvent())

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
