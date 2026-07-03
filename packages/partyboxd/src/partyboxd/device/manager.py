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
from partyboxd.device.events import (
    ConnectedEvent,
    DeviceEvent,
    DisconnectedEvent,
    EventBus,
    PowerChangedEvent,
)

log = logging.getLogger(__name__)

# How often to cross-check the transport's live connection state while
# waiting for a disconnect notification. Bounds how long the daemon can
# hold a stale "connected" snapshot when the disconnect callback never
# fires (e.g. after a bluetoothd restart — see _drain_with_health_check).
_HEALTH_CHECK_INTERVAL = 15.0

# Upper bound on the probe itself. A wedged Bluetooth stack can hang an
# ATT write instead of failing it; an unbounded probe would then hang the
# health check that exists to detect exactly that state.
_PROBE_TIMEOUT = 10.0


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

    def __init__(self, settings: SpeakerSettings) -> None:
        self._settings = settings
        self._snapshot: StatusSnapshot = _DISCONNECTED
        self._device: PartyBoxDevice | None = None
        self._bus = EventBus()

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

    async def get_volume(self) -> int | None:
        """Return the current hardware volume (0-100), or None if not readable.

        Raises:
            DeviceNotConnectedError: if the speaker is not connected.
        """
        device = self._device
        if device is None:
            raise DeviceNotConnectedError()
        try:
            level: int | None = await device.volume.get()
            return level
        except (ConnectionLostError, NotConnectedError) as exc:
            raise DeviceNotConnectedError() from exc
        except NotImplementedError:
            return None

    async def set_volume(self, percent: int) -> None:
        """Set the hardware volume (0-100).

        Raises:
            ValueError: if *percent* is outside [0, 100].
            DeviceNotConnectedError: if the speaker is not connected or the
                connection is lost during the command.
            NotImplementedError: if the BLE volume opcode is not yet confirmed.
        """
        device = self._device
        if device is None:
            raise DeviceNotConnectedError()
        try:
            await device.volume.set(percent)
        except (ConnectionLostError, NotConnectedError) as exc:
            raise DeviceNotConnectedError() from exc

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
            await self._drain_with_health_check(device)
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

    async def _drain_with_health_check(self, device: PartyBoxDevice) -> None:
        """Wait for disconnect, cross-checking the transport's live state.

        ``drain_until_disconnect()`` relies on the transport's disconnect
        callback. That callback never fires when bluetoothd itself goes away
        (a restarting BlueZ exits without emitting ``InterfacesRemoved``),
        which would leave the manager waiting forever on a dead connection
        while reporting ``connected: true``. Cached state (``is_connected``)
        is equally stale in that situation, so the check is an actual ATT
        round-trip: :meth:`PartyBoxDevice.verify_connection` fails on a dead
        link even when no disconnect was ever signalled.
        """
        drain = asyncio.create_task(device.drain_until_disconnect())
        try:
            while True:
                done, _ = await asyncio.wait({drain}, timeout=_HEALTH_CHECK_INTERVAL)
                if drain in done:
                    await drain  # propagates ConnectionLostError / NotConnectedError
                    return
                try:
                    await asyncio.wait_for(device.verify_connection(), timeout=_PROBE_TIMEOUT)
                except (ConnectionLostError, NotConnectedError, TimeoutError) as exc:
                    raise ConnectionLostError(
                        f"connection health probe failed (bluetoothd restart?): {exc}"
                    ) from exc
        finally:
            if not drain.done():
                drain.cancel()
                await asyncio.gather(drain, return_exceptions=True)

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
