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
from collections.abc import Awaitable, Callable
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

# Cap on the scan/connect retry backoff. A controller wedged by BCM4345 HCI
# UART corruption (ADR-028) can fail every scan or ConnectProfile call for
# minutes at a time; without a cap, `reconnect_delay` (5s default, flat, no
# backoff) retries continuously and adds BLE scan traffic to a controller
# already struggling — the same failure mode AudioService's A2DP retry loop
# hits on the Classic side. 60s matches AudioService's cap (companion's
# audio.py `_RETRY_MAX`) so neither loop hammers the shared radio faster than
# the other once both have backed off.
_RECONNECT_MAX = 60.0

# Consecutive scan→connect cycles where scanning FOUND the speaker but the
# connection failed, before the manager requests an adapter recovery.
# "Scanning works but connections fail" is the wedged-controller signature
# (ADR-023, ADR-039 — observed 2026-07-17: 22 straight failures over 25
# minutes until an adapter reset). Cycles where the scan comes up empty say
# nothing about the controller (the speaker is usually just off) and leave
# the counter untouched.
_WEDGE_CONNECT_FAILURES = 3

# Connect failures further apart than this don't accumulate toward the wedge
# threshold. An isolated failure is normal (the speaker resets its BLE stack
# after power commands, rotating private addresses go stale mid-connect); a
# wedge produces a dense run of them.
_WEDGE_WINDOW = 600.0

# Consecutive scan *errors* (Scanner.find raising — distinct from a clean
# empty scan, which just means the speaker is off) before requesting adapter
# recovery. Scanning itself failing means the adapter is unusable, e.g.
# powered off after a half-completed recovery — a state the connect-failure
# counter can never see because no connect is ever attempted. Higher
# threshold than _WEDGE_CONNECT_FAILURES: transient scan errors also occur
# while bluetoothd re-initializes right after a recovery power-cycle.
_WEDGE_SCAN_ERRORS = 5

# Consecutive *clean-but-empty* scans before the injected stale-connection
# reclaim runs. An empty scan usually just means the speaker is off — but it
# is also the only Pi-side symptom of an orphaned LE connection: when a
# previous companion process died without disconnecting (crash, SIGKILL,
# power loss, or a shutdown path that never ran), bluetoothd keeps the link
# up, the speaker stops advertising its connectable set, and every scan comes
# back clean-but-empty indefinitely (observed 2026-07-17: 60+ empty scans
# over 30+ minutes while the Pi's own adapter held the stale link). Three
# cycles keeps the reclaim check off the fast path while still healing the
# orphan within roughly a minute of a cold start.
_RECLAIM_EMPTY_SCANS = 3

# Minimum time between *unsuccessful* reclaim attempts. The connect-failure
# path consults the reclaim before every counted failure; if some unexpected
# BlueZ or firmware state produces a dense run of failures with nothing to
# reclaim, this keeps the helper from re-enumerating the bus on every one.
# Successful reclaims are never rate-limited — they carry real signal.
_RECLAIM_COOLDOWN = 30.0

# Connect failures within this window after a power command are not counted:
# the speaker resets its own BLE stack for ~15-17 s after every power on/off
# (ADR-034), during which dense "could not connect" failures are expected and
# say nothing about the controller.
_POWER_COMMAND_GRACE = 60.0

# Minimum time between adapter recoveries. A recovery power-cycle drops every
# connection on the adapter, including a possibly healthy A2DP stream — if
# one didn't clear the failure mode, repeating it every few minutes turns a
# broken control plane into broken audio as well.
_RECOVERY_COOLDOWN = 900.0


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

    def __init__(
        self,
        settings: SpeakerSettings,
        *,
        adapter_recover_fn: Callable[[], Awaitable[bool]] | None = None,
        stale_reclaim_fn: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        """*adapter_recover_fn*, when provided, is called after
        ``_WEDGE_CONNECT_FAILURES`` dense connect failures (see
        :meth:`_note_connect_failure`) to recover a wedged controller —
        typically a Bluetooth adapter power-cycle. It returns True if the
        recovery action completed. ``None`` (the default, and standalone
        partyboxd's configuration) disables self-healing; the manager then
        just keeps retrying with backoff as before.

        *stale_reclaim_fn*, when provided, disconnects an orphaned LE link to
        the speaker left behind by a previous process. It is consulted after
        ``_RECLAIM_EMPTY_SCANS`` consecutive clean-but-empty scans (the
        orphan suppresses advertising) and on every connect failure before it
        counts toward wedge recovery (the orphan holds the speaker's single
        control slot while it keeps advertising — see
        :meth:`_note_connect_failure`). It returns True if it disconnected
        something — the manager then retries without backoff, since the
        speaker becomes connectable again as soon as the stale link drops.

        Invariant: *stale_reclaim_fn* is only ever called while the manager
        holds no active control connection (both call sites live in the
        scan/connect path, which runs only while ``_device is None``), so a
        reclaim can never disconnect the manager's own live link.
        """
        self._settings = settings
        self._snapshot: StatusSnapshot = _DISCONNECTED
        self._device: PartyBoxDevice | None = None
        self._bus: EventBus[DeviceEvent] = EventBus()
        #: Consecutive liveness-probe misses on the current connection.
        self._liveness_misses = 0
        #: Set while `_device` is non-None; lets callers await a reconnect
        #: instead of failing instantly during a transient BLE drop.
        self._connected_event = asyncio.Event()
        #: Current scan/connect retry delay; doubles on failure up to
        #: `_RECONNECT_MAX`, resets to `settings.reconnect_delay` on success.
        self._retry_delay = settings.reconnect_delay
        self._adapter_recover_fn = adapter_recover_fn
        self._stale_reclaim_fn = stale_reclaim_fn
        #: Consecutive clean-but-empty scans (reset by any scan that finds
        #: the speaker; see _note_empty_scan).
        self._empty_scans = 0
        #: loop.time() of the last reclaim attempt that found nothing
        #: (cool-down gate; None after a successful reclaim).
        self._last_failed_reclaim: float | None = None
        #: Dense connect failures since the last success (see _WEDGE_WINDOW).
        self._connect_failures = 0
        self._last_connect_failure = 0.0
        #: Consecutive Scanner.find exceptions (reset by any completed scan).
        self._scan_errors = 0
        #: loop.time() of the last power command sent (ADR-034 grace window).
        self._last_power_command: float | None = None
        #: loop.time() of the last adapter recovery (cool-down gate).
        self._last_recovery: float | None = None

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
        # Opens the ADR-034 grace window: the speaker now resets its BLE
        # stack, and the connect failures that produces must not count as
        # wedge evidence (see _note_connect_failure).
        self._last_power_command = asyncio.get_running_loop().time()
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
        # Same ADR-034 grace window as power_on (see _note_connect_failure).
        self._last_power_command = asyncio.get_running_loop().time()
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
            await self._sleep_and_backoff()
            return

        try:
            await device.connect()
        except ConnectionFailedError as exc:
            log.warning("connection failed (attempt %d): %s", attempt, exc)
            await self._note_connect_failure()
            await self._sleep_and_backoff()
            return

        self._retry_delay = self._settings.reconnect_delay
        self._connect_failures = 0
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
            # A false-positive I/O timeout can end this cycle while BlueZ
            # still holds a live connection (the speaker accepts only one);
            # dropping the client without disconnecting would orphan that
            # connection and block every future reconnect. Bounded inside the
            # transport (_GATT_IO_TIMEOUT); a no-op when already disconnected.
            try:
                await device.disconnect()
            except Exception as exc:
                log.debug("cleanup disconnect failed: %s", exc)
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
            try:
                done, _ = await asyncio.wait({drain}, timeout=_HEALTH_CHECK_INTERVAL)
            except asyncio.CancelledError:
                # Shutdown lands here (asyncio.wait does not cancel its
                # awaitables). Reap the drain task, or the cleanup
                # disconnect makes it raise NotConnectedError into the void
                # ("Task exception was never retrieved" noise at exit).
                drain.cancel()
                await asyncio.gather(drain, return_exceptions=True)
                raise
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

    async def _note_connect_failure(self) -> None:
        """Track a scan-found-it-but-connect-failed cycle; self-heal on a dense run.

        The counter only moves on this specific failure shape: the scan saw
        the speaker (so the radio receives fine and the speaker is on) but the
        connection could not be established. A dense run of those is the
        wedged-controller signature (ADR-039); once it reaches
        ``_WEDGE_CONNECT_FAILURES``, recovery is requested via
        :meth:`_maybe_recover`. Failures further apart than ``_WEDGE_WINDOW``
        restart the count, so occasional one-offs spread over days never add
        up to a spurious recovery, and failures inside the ADR-034 window
        after a power command are not counted at all — the speaker resets its
        own BLE stack then, and dense failures are the *expected* shape.
        """
        now = asyncio.get_running_loop().time()
        if (
            self._last_power_command is not None
            and now - self._last_power_command < _POWER_COMMAND_GRACE
        ):
            log.debug("connect failure inside post-power-command grace window; not counted")
            return
        # An orphaned LE link can also express as connect refusal rather than
        # absent advertising: the speaker keeps advertising fresh rotating
        # addresses while its single control slot is held by the stale link
        # (observed 2026-07-18), so every connect fails. That is not
        # controller-wedge evidence — reclaiming the stale link fixes it,
        # while counting it toward _WEDGE_CONNECT_FAILURES escalates to an
        # adapter power-cycle that drops live audio for nothing.
        if await self._reclaim_stale_link():
            return
        if now - self._last_connect_failure > _WEDGE_WINDOW:
            self._connect_failures = 0
        self._last_connect_failure = now
        self._connect_failures += 1
        if self._connect_failures >= _WEDGE_CONNECT_FAILURES:
            await self._maybe_recover(
                f"{self._connect_failures} dense connect failures while scanning still works"
                " (suspected controller wedge)"
            )

    async def _note_empty_scan(self) -> None:
        """Track a clean scan that found nothing; reclaim a stale LE link on a run.

        A connected LE PartyBox device on our own adapter while this manager
        holds no connection is stale by definition — any *other* holder of
        the speaker's control channel (a phone running the JBL app, say)
        never shows up on the Pi's adapter at all. The injected
        *stale_reclaim_fn* checks for exactly that and disconnects it.
        Harmless when the speaker is genuinely off: the check finds nothing
        and the scan loop continues as before. Counting only *clean* empties
        keeps this orthogonal to ``_note_scan_error`` (adapter unusable) and
        ``_note_connect_failure`` (controller wedge).
        """
        if self._stale_reclaim_fn is None:
            return
        self._empty_scans += 1
        if self._empty_scans < _RECLAIM_EMPTY_SCANS:
            return
        self._empty_scans = 0
        await self._reclaim_stale_link()

    async def _reclaim_stale_link(self) -> bool:
        """Run the injected stale-link reclaim; True if it disconnected something.

        On success the retry backoff is reset: the speaker's control slot is
        free again, so the very next cycle should find and connect it.
        Unsuccessful attempts are rate-limited by ``_RECLAIM_COOLDOWN``.
        Never raises — a failed reclaim just leaves the retry loop as it was.
        """
        if self._stale_reclaim_fn is None:
            return False
        now = asyncio.get_running_loop().time()
        if (
            self._last_failed_reclaim is not None
            and now - self._last_failed_reclaim < _RECLAIM_COOLDOWN
        ):
            return False
        try:
            reclaimed = await self._stale_reclaim_fn()
        except Exception as exc:
            log.warning("stale-connection reclaim raised: %s", exc)
            self._last_failed_reclaim = now
            return False
        if not reclaimed:
            self._last_failed_reclaim = now
            return False
        self._last_failed_reclaim = None
        log.warning(
            "disconnected a stale LE connection to the speaker "
            "(orphaned by a previous process); rescanning without backoff"
        )
        self._retry_delay = self._settings.reconnect_delay
        return True

    async def _note_scan_error(self) -> None:
        """Track a scan that *errored* (as opposed to finding nothing).

        Scanner.find raising means the adapter itself is unusable — most
        importantly the powered-off state left behind by a half-completed
        recovery, which the connect-failure counter can never observe because
        no connect is ever attempted. Requesting recovery here closes that
        loop: the power-cycle's Powered=true write brings the adapter back.
        """
        self._scan_errors += 1
        if self._scan_errors >= _WEDGE_SCAN_ERRORS:
            await self._maybe_recover(
                f"{self._scan_errors} consecutive scan errors (Bluetooth adapter unusable)"
            )

    async def _maybe_recover(self, reason: str) -> None:
        """Run the injected adapter recovery, rate-limited by _RECOVERY_COOLDOWN.

        The cool-down is the storm brake: a recovery power-cycle drops every
        connection on the adapter (including healthy A2DP audio), so a failure
        mode that recovery does not fix — a speaker-side LE fault, say — must
        not translate into an adapter cycle every few minutes forever. While
        suppressed, the failure counters keep their value, so the next failure
        after the cool-down expires re-triggers immediately.
        """
        if self._adapter_recover_fn is None:
            return
        now = asyncio.get_running_loop().time()
        if self._last_recovery is not None and now - self._last_recovery < _RECOVERY_COOLDOWN:
            log.debug("adapter recovery suppressed by cool-down (%s)", reason)
            return
        self._last_recovery = now
        self._connect_failures = 0
        self._scan_errors = 0
        log.warning("requesting Bluetooth adapter recovery: %s (ADR-039)", reason)
        try:
            recovered = await self._adapter_recover_fn()
        except Exception as exc:
            log.warning("adapter recovery raised: %s", exc)
            return
        if recovered:
            log.info("adapter recovery completed; resuming connect attempts")
        else:
            log.warning("adapter recovery reported failure; resuming connect attempts anyway")

    async def _sleep_and_backoff(self) -> None:
        """Sleep the current scan/connect retry delay, then grow it for next time.

        Resets to `settings.reconnect_delay` on the next successful connect
        (see `_connect_and_maintain`) — a transient failure doesn't leave a
        stale slow delay in place once the speaker is reachable again.
        """
        await asyncio.sleep(self._retry_delay)
        self._retry_delay = min(self._retry_delay * 2, _RECONNECT_MAX)

    async def _scan(self) -> PartyBoxDevice | None:
        log.info("scanning for speaker")
        try:
            device = await Scanner.find(timeout=self._settings.scan_timeout)
        except Exception as exc:
            log.warning("scan failed: %s", exc)
            await self._note_scan_error()
            return None
        self._scan_errors = 0
        if device is None:
            await self._note_empty_scan()
        else:
            self._empty_scans = 0
        return device

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
