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
from dataclasses import dataclass, replace
from typing import Literal

from partybox import (
    BatteryStatusResponse,
    ConnectionFailedError,
    ConnectionLostError,
    PartyBoxDevice,
    Scanner,
)
from partybox.bluetooth.transport import NotConnectedError

from partyboxd.config import SpeakerSettings
from partyboxd.device.events import (
    ConnectedEvent,
    DeviceEvent,
    DisconnectedEvent,
    EventBus,
    PowerChangedEvent,
    SpeakerStateChangedEvent,
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

# Consecutive liveness-probe misses (battery, falling back to firmware)
# before the speaker is marked asleep. It stops answering control queries in
# standby while staying BLE-connected, so a sustained run of misses on every
# probe we have means "asleep". A small tolerance avoids flipping state on a
# single transient miss while the speaker is genuinely awake.
_LIVENESS_MISS_LIMIT = 2

# How long a power command's caller waits for the manager to land a fresh
# connection when the speaker is mid-reconnect. Sending a power on/off command
# makes the PartyBox reset its own BLE stack for a stretch before it
# reconnects (observed ~15-17s on hardware, even on mains power) — see
# ADR-034. Without this wait, "turn off, then immediately turn back on" would
# race the reconnect and fail with DeviceNotConnectedError nearly every time.
_RECONNECT_WAIT_TIMEOUT = 20.0


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
    #: Full battery reading (charging source, health, capacities), or None when
    #: the speaker has no battery. ``battery`` above is its derived percentage.
    battery_status: BatteryStatusResponse | None = None
    #: Whether the connected speaker has a battery capability at all — known
    #: once confirmed (``PartyBoxDevice.battery is not None`` after a
    #: successful probe), independent of whether it is currently reporting a
    #: value. ``False`` while disconnected, or while a battery-capable
    #: speaker's capability has simply never been confirmed yet (e.g. it was
    #: already asleep for the whole BLE session — see ``speaker_awake``).
    has_battery: bool = False
    #: Whether the speaker most recently answered *any* control query
    #: (battery or, as a fallback, firmware) — the awake/standby signal.
    #: Deliberately independent of ``has_battery``: gating standby on a
    #: confirmed battery reading would never detect standby on a speaker
    #: that has been asleep for its *entire* BLE session, since capability
    #: detection itself requires an answer. Firmware is a universal probe
    #: (all known PartyBox models answer it), so it still proves liveness
    #: even before — or without ever — confirming battery capability.
    speaker_awake: bool = True

    @property
    def speaker_state(self) -> Literal["off", "standby", "on"]:
        """Coarse power state, derived — never stored redundantly."""
        if not self.connected:
            return "off"
        if not self.speaker_awake:
            return "standby"
        return "on"


_DISCONNECTED = StatusSnapshot(
    connected=False,
    address=None,
    firmware=None,
    battery=None,
    battery_status=None,
    has_battery=False,
    speaker_awake=True,
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
        #: Consecutive liveness-probe misses on the current connection.
        self._liveness_misses = 0
        #: Set while `_device` is non-None; lets callers await a reconnect
        #: instead of failing instantly during a transient BLE drop.
        self._connected_event = asyncio.Event()

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

    def _set_snapshot(self, snapshot: StatusSnapshot) -> None:
        """Update the snapshot, emitting SpeakerStateChangedEvent on transition.

        The single choke point for snapshot mutation, so ``speaker_state``
        transitions (off/standby/on) are never missed regardless of which
        code path produced the new snapshot.
        """
        prev_state = self._snapshot.speaker_state
        self._snapshot = snapshot
        new_state = snapshot.speaker_state
        if new_state != prev_state:
            self._bus.emit(SpeakerStateChangedEvent(state=new_state))

    async def _get_connected_device(self) -> PartyBoxDevice:
        """Return the connected device, waiting briefly for an in-progress reconnect.

        A power command makes the PartyBox reset its own BLE stack for a
        stretch before it reconnects (see ``_RECONNECT_WAIT_TIMEOUT``), so a
        caller landing in that window should not see an instant failure —
        it waits for the manager's connect loop to land a fresh connection
        instead.

        Raises:
            DeviceNotConnectedError: if no connection is established within
                ``_RECONNECT_WAIT_TIMEOUT``.
        """
        if self._device is not None:
            return self._device
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=_RECONNECT_WAIT_TIMEOUT)
        except TimeoutError:
            raise DeviceNotConnectedError() from None
        if self._device is None:
            raise DeviceNotConnectedError()
        return self._device

    async def power_on(self) -> None:
        """Send a power-on command to the connected speaker.

        Waits for an in-progress reconnect (see ``_get_connected_device``)
        before giving up.

        Raises :exc:`DeviceNotConnectedError` if the speaker is not connected
        or if the connection is lost during the command.
        """
        device = await self._get_connected_device()
        try:
            await device.power.turn_on()
        except (ConnectionLostError, NotConnectedError) as exc:
            raise DeviceNotConnectedError() from exc
        self._bus.emit(PowerChangedEvent(state="on"))

    async def get_volume(self) -> int | None:
        """Return the current hardware volume (0-100), or None if not readable.

        Waits for an in-progress reconnect (see ``_get_connected_device``)
        before giving up.

        Raises:
            DeviceNotConnectedError: if the speaker is not connected.
        """
        device = await self._get_connected_device()
        try:
            level: int | None = await device.volume.get()
            return level
        except (ConnectionLostError, NotConnectedError) as exc:
            raise DeviceNotConnectedError() from exc
        except NotImplementedError:
            return None

    async def set_volume(self, percent: int) -> None:
        """Set the hardware volume (0-100).

        Waits for an in-progress reconnect (see ``_get_connected_device``)
        before giving up.

        Raises:
            ValueError: if *percent* is outside [0, 100].
            DeviceNotConnectedError: if the speaker is not connected or the
                connection is lost during the command.
            NotImplementedError: if the BLE volume opcode is not yet confirmed.
        """
        device = await self._get_connected_device()
        try:
            await device.volume.set(percent)
        except (ConnectionLostError, NotConnectedError) as exc:
            raise DeviceNotConnectedError() from exc

    async def power_off(self) -> None:
        """Send a power-off command to the connected speaker.

        Waits for an in-progress reconnect (see ``_get_connected_device``)
        before giving up.

        Raises :exc:`DeviceNotConnectedError` if the speaker is not connected
        or if the connection is lost during the command.
        """
        device = await self._get_connected_device()
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
        self._connected_event.set()
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
            self._connected_event.clear()
            self._set_snapshot(_DISCONNECTED)
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
        while True:
            drain = asyncio.create_task(device.drain_until_disconnect())
            done, _ = await asyncio.wait({drain}, timeout=_HEALTH_CHECK_INTERVAL)
            if drain in done:
                await drain  # propagates ConnectionLostError / NotConnectedError
                return
            # Interval elapsed and the link still looks alive. The transport
            # supports only ONE notification consumer at a time (a single
            # receive() reader); drain_until_disconnect() is currently that
            # consumer. Cancel it to reclaim the reader before we issue any
            # request/response round-trip (the liveness probe, then the battery
            # refresh) — otherwise the response would race into the drain and be
            # discarded. Drain is recreated at the top of the next iteration.
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)
            try:
                await asyncio.wait_for(device.verify_connection(), timeout=_PROBE_TIMEOUT)
            except (ConnectionLostError, NotConnectedError, TimeoutError) as exc:
                raise ConnectionLostError(
                    f"connection health probe failed (bluetoothd restart?): {exc}"
                ) from exc
            await self._poll_liveness(device)

    async def _poll_liveness(self, device: PartyBoxDevice) -> None:
        """Probe whether the speaker is awake by trying battery, then firmware.

        All known PartyBox models answer firmware queries when awake; battery is
        a better signal (more direct), but is not universally supported. Trying
        both lets us detect standby even on a speaker that was already asleep
        when the BLE connection was established (before battery capability could
        ever be confirmed). A speaker that is asleep simply does not answer — that
        is not an error, so it is swallowed. Genuine connection loss propagates so
        the maintain loop reconnects.
        """
        awake = False
        battery_status_new: BatteryStatusResponse | None = None
        has_battery_new = False

        # Try battery first (better signal, more specific to power state).
        try:
            capability = device.battery
            if capability is None:
                capability = await device.redetect_battery()
            if capability is not None:
                status = await capability.status()
                has_battery_new = True
                battery_status_new = status
                awake = True
        except ConnectionLostError, NotConnectedError:
            raise
        except TimeoutError:
            pass  # Speaker did not answer; try firmware next.

        # Fall back to firmware query (universal probe — all models answer
        # this when awake, even if they have no battery capability).
        if not awake:
            try:
                await device.device_info.firmware_version()
                awake = True
            except ConnectionLostError, NotConnectedError:
                raise
            except TimeoutError, NotImplementedError:
                pass  # Firmware also timed out or is not implemented.

        # Count consecutive misses on both probes combined.
        if not awake:
            self._liveness_misses += 1
            if self._liveness_misses >= _LIVENESS_MISS_LIMIT:
                log.info(
                    "speaker not answering liveness probes (battery/firmware); marking standby"
                )
                self._set_snapshot(
                    replace(self._snapshot, speaker_awake=False, battery=None, battery_status=None)
                )
            log.debug("liveness probe timeout (speaker likely in standby)")
            return

        # Speaker answered — update state.
        self._liveness_misses = 0
        prev = self._snapshot
        new_snapshot = prev

        # Battery: update has_battery if newly confirmed, and battery reading if changed.
        if has_battery_new and not prev.has_battery:
            new_snapshot = replace(new_snapshot, has_battery=True)
        if battery_status_new is not None:
            level = battery_status_new.charge_percent
            if level != prev.battery or battery_status_new != prev.battery_status:
                if prev.battery is None and level is not None:
                    log.info("battery recovered on re-probe: %d%%", level)
                new_snapshot = replace(
                    new_snapshot, battery=level, battery_status=battery_status_new
                )

        # Awake: ensure speaker_awake is set if it wasn't already.
        if not prev.speaker_awake:
            new_snapshot = replace(new_snapshot, speaker_awake=True)

        if new_snapshot is not prev:
            self._set_snapshot(new_snapshot)

    async def _scan(self) -> PartyBoxDevice | None:
        log.info("scanning for speaker")
        try:
            return await Scanner.find(timeout=self._settings.scan_timeout)
        except Exception as exc:
            log.warning("scan failed: %s", exc)
            return None

    async def _refresh(self, device: PartyBoxDevice) -> None:
        """Query initial device state and update the snapshot."""
        self._liveness_misses = 0
        firmware: str | None = None
        battery: int | None = None
        battery_status: BatteryStatusResponse | None = None
        battery_capability = device.battery
        has_battery = battery_capability is not None
        speaker_awake = False

        try:
            firmware = await device.device_info.firmware_version()
            log.info("firmware version: %s", firmware)
            speaker_awake = True
        except Exception as exc:
            log.warning("could not read firmware version: %s", exc)

        try:
            if battery_capability is not None:
                battery_status = await battery_capability.status()
                battery = battery_status.charge_percent
                speaker_awake = True
                log.info(
                    "battery: %s%% (%s)",
                    battery,
                    battery_status.charging_status.name.lower()
                    if battery_status.charging_status is not None
                    else "unknown source",
                )
        except Exception as exc:
            log.warning("could not read battery status: %s", exc)

        self._set_snapshot(
            StatusSnapshot(
                connected=True,
                address=device.address,
                firmware=firmware,
                battery=battery,
                battery_status=battery_status,
                has_battery=has_battery,
                speaker_awake=speaker_awake,
            )
        )

    async def _disconnect(self) -> None:
        if self._device is not None:
            try:
                await self._device.disconnect()
            except Exception as exc:
                log.warning("error during disconnect: %s", exc)
            finally:
                self._device = None
                self._connected_event.clear()
                self._set_snapshot(_DISCONNECTED)
