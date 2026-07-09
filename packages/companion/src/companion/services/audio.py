"""Bluetooth A2DP audio sink manager.

Maintains the Bluetooth Classic A2DP connection between the Pi and the
speaker. Without this connection PipeWire has no Bluetooth audio sink and
librespot has nowhere to send audio.

The BLE GATT control connection (managed by DeviceManager) and the A2DP audio
connection are separate Bluetooth subsystems. Both can coexist on the same
controller — confirmed in M3.

AudioService owns the ``audio_ready`` concept — whether the appliance is
currently capable of producing audio — and publishes changes via a subscription
bus so that other services can react without polling and without knowing about
Bluetooth internals.  See ADR-026.

**Expected idle state — ``Connected: no`` is normal.** A2DP is a
connection-oriented BR/EDR profile: the ACL link is established when needed
and released when idle. JBL PartyBox speakers drop the BR/EDR link after a
period of inactivity (no audio). ``AudioService`` runs a retry loop that
re-establishes A2DP before audio is needed; the ``/api/v1/audio`` endpoint
reflects the *current* link state, not whether bonding is intact. A bonded,
powered-on speaker that reports ``connected: false`` is in the expected idle
state — the service will reconnect automatically on the next check cycle.

The BLE GATT control connection (used by the ``partybox`` SDK for EQ, power,
etc.) is also established on-demand and is unrelated to this A2DP state.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Literal

from partyboxd.eventbus import EventBus

from companion.config import AudioSettings
from companion.services._a2dp_connect import STALE_BOND_CODE, error_code
from companion.services.bluez_dbus import BluezClient

log = logging.getLogger(__name__)

_CHECK_INTERVAL = 60.0  # seconds between health checks when connected
_POST_CONNECT_SETTLE = 5.0  # wait after ConnectProfile ok before re-checking
_RETRY_BASE = 10.0  # initial retry delay after a failed/lost connection
_RETRY_MAX = 60.0  # cap backoff at 60 s — 5 min was too slow to recover

# Flap protection: a controller exhibiting HCI transport errors (see ADR-028
# "WirePlumber endpoint degradation investigation") can accept ConnectProfile
# repeatedly while the resulting MediaTransport1 is torn down seconds later.
# Without this, each reconnect is treated as an independent success and the
# retry loop hammers the controller at full speed, adding to the traffic that
# provoked the errors in the first place.
_FLAP_WINDOW = 20.0  # a connection lasting less than this counts as a flap
_FLAP_LIMIT = 3  # consecutive flaps before an extended cooldown
_FLAP_COOLDOWN = 120.0  # backoff applied once flapping is detected

# Sustained-failure protection: the same BCM4345 HCI corruption (ADR-028) can
# also make ConnectProfile itself fail outright on every attempt instead of
# succeeding-then-dropping. That case never sets audio_ready True, so the flap
# counter above never moves — the loop just doubles retry_delay up to
# _RETRY_MAX and then sits there retrying every 60s indefinitely, which is the
# same "retry traffic adds to the traffic that provoked the errors" problem
# the flap cooldown addresses, just on the other side of a successful connect.
_FAILURE_LIMIT = 5  # consecutive outright connect failures before a cooldown
_FAILURE_COOLDOWN = 300.0  # backoff applied once sustained failure is detected


# ---------------------------------------------------------------------------
# Audio events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AudioReadyChanged:
    """Emitted when audio readiness transitions between ready and not ready.

    ``audio_ready`` is ``True`` when A2DP is connected and the appliance
    can produce audio; ``False`` when the A2DP link is absent. ``address``
    is the current sink address (or ``None``), included so WS subscribers
    (the Portal, via the merged event stream) can update their view of
    ``GET /api/v1/audio`` without an extra round-trip — see
    docs/adr/035-state-ownership-and-signal-pipeline.md.

    Subscribers receive this event on every transition.  Events are NOT
    emitted when the state is unchanged (e.g. if the connection check
    confirms an existing connection, no event fires).
    """

    audio_ready: bool
    address: str | None = None
    type: Literal["audio_changed"] = "audio_changed"


type AudioServiceEvent = AudioReadyChanged


# ---------------------------------------------------------------------------
# AudioStatus (snapshot)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AudioStatus:
    """Point-in-time snapshot of A2DP audio connection state."""

    connected: bool
    address: str | None


# ---------------------------------------------------------------------------
# AudioService
# ---------------------------------------------------------------------------


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

    ``audio_ready`` reflects whether the appliance can currently produce audio
    (A2DP connected and operational).  Subscribers receive
    :class:`AudioReadyChanged` events on every state transition; see
    :meth:`subscribe`.  This abstraction is intentionally separate from BLE
    GATT connectivity — see ADR-026.
    """

    def __init__(self, settings: AudioSettings) -> None:
        self._address: str | None = settings.sink_address
        self._audio_ready = False
        self._address_ready = asyncio.Event()
        self._bus: EventBus[AudioServiceEvent] = EventBus()
        self._reconnect_now = asyncio.Event()
        self._connected_at: float | None = None
        self._flap_count = 0
        self._consecutive_failures = 0
        if self._address is not None:
            self._address_ready.set()

    @property
    def audio_ready(self) -> bool:
        """True when A2DP is connected and the appliance can produce audio."""
        return self._audio_ready

    @property
    def status(self) -> AudioStatus:
        """Point-in-time snapshot. ``connected`` reflects :attr:`audio_ready`."""
        return AudioStatus(connected=self._audio_ready, address=self._address)

    def subscribe(self) -> asyncio.Queue[AudioServiceEvent]:
        """Subscribe to audio readiness changes.

        Returns a queue pre-populated with the **current state** as its first
        event, followed by :class:`AudioReadyChanged` events for all future
        transitions.  This means callers never need to read :attr:`audio_ready`
        separately — processing the queue alone is sufficient and race-free.

        Call :meth:`unsubscribe` with the returned queue when done.

        Intended for M17.3 Spotify lifecycle gating and other consumers that
        must react to audio availability without polling.
        """
        q = self._bus.subscribe()
        q.put_nowait(AudioReadyChanged(audio_ready=self._audio_ready, address=self._address))
        return q

    def unsubscribe(self, queue: asyncio.Queue[AudioServiceEvent]) -> None:
        """Stop delivering events to *queue*."""
        self._bus.unsubscribe(queue)

    def recheck_now(self) -> None:
        """Interrupt an idle wait and re-check the A2DP link immediately.

        The connected-idle branch of :meth:`run` only re-checks every
        ``_CHECK_INTERVAL`` (60s) — deliberately lazy for a link that isn't
        expected to change on its own (ADR-028). But the BLE control link
        going into standby is a strong signal that the audio link dropped at
        the same time (they're the same speaker), and waiting out the rest of
        that 60s window makes the Portal show a stale "connected" status for
        up to a minute after the speaker visibly went idle. Callers that
        observe such a signal (see ``_recheck_audio_on_standby`` in
        ``companion.__main__``) call this to short-circuit the wait instead.
        """
        self._reconnect_now.set()

    def update_address(self, address: str) -> None:
        """Set or update the A2DP sink address and interrupt any backoff sleep.

        Called by PairingService after a successful pairing.  When called while
        the service is sleeping between failed connect attempts, the sleep is
        cut short and a fresh attempt begins immediately (backoff reset).
        """
        self._address = address
        self._address_ready.set()
        self._reconnect_now.set()

    def forget(self) -> None:
        """Clear the sink address and quiesce the connection loop (factory reset).

        The counterpart to :meth:`update_address`: it un-sets the address so the
        run loop stops trying to reach a speaker whose bond has been removed and
        returns to waiting for a fresh pairing.  Interrupts any in-progress
        backoff sleep so the loop re-evaluates immediately.
        """
        self._address = None
        self._address_ready.clear()
        self._reconnect_now.set()
        self._consecutive_failures = 0
        self._set_audio_ready(False)

    async def run(self) -> None:
        """Ensure A2DP is connected; reconnect on drop. Runs until cancelled.

        If no address is configured, waits until :meth:`update_address` is
        called rather than returning immediately.  This keeps the Supervisor
        from treating a no-address startup as an unexpected exit.

        Emits :class:`AudioReadyChanged` whenever ``audio_ready`` transitions.
        """
        if not self._address_ready.is_set():
            log.info("A2DP: no sink address configured; waiting for pairing")
            await self._address_ready.wait()

        log.info("Audio service starting (sink=%s)", self._address)
        retry_delay = _RETRY_BASE
        try:
            while True:
                if not self._address_ready.is_set():
                    # forget() cleared the sink (factory reset): stop chasing a
                    # speaker whose bond is gone and wait for a fresh pairing.
                    self._set_audio_ready(False)
                    log.info("A2DP: sink address cleared; waiting for pairing")
                    await self._address_ready.wait()
                    log.info("Audio service resuming (sink=%s)", self._address)
                    retry_delay = _RETRY_BASE
                    self._consecutive_failures = 0
                    continue
                if not await self._is_connected():
                    self._set_audio_ready(False)
                    if self._flap_count >= _FLAP_LIMIT:
                        log.warning(
                            "A2DP flapping detected (%d short-lived connections to %s)"
                            " — cooling down %.0fs instead of retrying immediately",
                            self._flap_count,
                            self._address,
                            _FLAP_COOLDOWN,
                        )
                        self._flap_count = 0
                        if await self._wait_retry(_FLAP_COOLDOWN):
                            log.info("A2DP: re-pair detected, retrying immediately")
                        retry_delay = _RETRY_BASE
                        continue
                    log.info(
                        "A2DP sink not connected, connecting to %s (retry in %.0fs)",
                        self._address,
                        retry_delay,
                    )
                    if await self._connect():
                        # ConnectProfile returned ok — trust it and wait briefly
                        # before the top-of-loop _is_connected() check.
                        # MediaTransport1 is created asynchronously after
                        # ConnectProfile returns; without this settle sleep the
                        # check runs before the transport object appears in
                        # GetManagedObjects and would immediately re-trigger a
                        # connect attempt, hammering the speaker.
                        self._set_audio_ready(True)
                        retry_delay = _RETRY_BASE
                        self._consecutive_failures = 0
                        await asyncio.sleep(_POST_CONNECT_SETTLE)
                        continue
                    # connect failed — still check in case speaker auto-connected
                    if await self._is_connected():
                        retry_delay = _RETRY_BASE
                        self._consecutive_failures = 0
                        continue
                    self._consecutive_failures += 1
                    if self._consecutive_failures >= _FAILURE_LIMIT:
                        log.warning(
                            "A2DP: %d consecutive outright connect failures to %s"
                            " — cooling down %.0fs instead of retrying immediately",
                            self._consecutive_failures,
                            self._address,
                            _FAILURE_COOLDOWN,
                        )
                        self._consecutive_failures = 0
                        if await self._wait_retry(_FAILURE_COOLDOWN):
                            log.info("A2DP: re-pair detected, retrying immediately")
                        retry_delay = _RETRY_BASE
                        continue
                    if await self._wait_retry(retry_delay):
                        log.info("A2DP: re-pair detected, retrying immediately")
                        retry_delay = _RETRY_BASE
                    else:
                        retry_delay = min(retry_delay * 2, _RETRY_MAX)
                else:
                    if retry_delay > _RETRY_BASE:
                        log.info("A2DP connection stable, backoff reset")
                    self._set_audio_ready(True)
                    retry_delay = _RETRY_BASE
                    self._consecutive_failures = 0
                    # Interruptible so recheck_now() can cut this short — see
                    # its docstring for why.
                    await self._wait_retry(_CHECK_INTERVAL)
        except asyncio.CancelledError:
            self._set_audio_ready(False)
            log.info("Audio service stopping")
            raise

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _set_audio_ready(self, val: bool) -> None:
        """Update audio_ready and emit AudioReadyChanged on any transition.

        Also tracks flapping — see the `_FLAP_*` constants — by comparing how
        long the connection lasted against `_FLAP_WINDOW`.
        """
        if val == self._audio_ready:
            return
        self._audio_ready = val
        now = time.monotonic()
        if val:
            self._connected_at = now
        else:
            if self._connected_at is not None and now - self._connected_at < _FLAP_WINDOW:
                self._flap_count += 1
            else:
                self._flap_count = 0
            self._connected_at = None
        self._bus.emit(AudioReadyChanged(audio_ready=val, address=self._address))

    async def _is_connected(self) -> bool:
        assert self._address is not None
        ok, _ = await self._run_subprocess(self._address, "check")
        return ok

    @staticmethod
    async def _run_subprocess(address: str, command: str = "connect") -> tuple[bool, str]:
        """Run an A2DP helper command in a subprocess.

        bleak holds its own dbus-fast MessageBus in the asyncio loop.  Running
        BlueZ calls in a subprocess avoids any interaction between the two buses.

        command="connect" — returns (True, "") on success, (False, msg) on failure.
        command="check"   — returns (True, "") if connected, (False, "") if not.
        """
        import sys as _sys

        timeout = 35.0 if command == "connect" else 10.0
        try:
            proc = await asyncio.create_subprocess_exec(
                _sys.executable,
                "-m",
                "companion.services._a2dp_connect",
                address,
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return False, f"subprocess error: {exc}"
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return False, "subprocess timed out"
        line = stdout.decode(errors="replace").strip()
        if stderr:
            log.debug(
                "A2DP %s subprocess stderr: %s", command, stderr.decode(errors="replace").strip()
            )
        if command == "check":
            return line == "true", ""
        if line == "ok":
            return True, ""
        if not line and stderr:
            return False, "stderr: " + stderr.decode(errors="replace").strip()
        return False, line

    async def _wait_retry(self, delay: float) -> bool:
        """Sleep delay seconds; return True if woken early by update_address().

        Clears _reconnect_now before waiting so only a call made AFTER this
        point (i.e. a new pairing) can interrupt the sleep.
        """
        self._reconnect_now.clear()
        try:
            await asyncio.wait_for(self._reconnect_now.wait(), timeout=delay)
            return True
        except TimeoutError:
            return False

    async def _connect(self) -> bool:
        """Attempt A2DP connection.  Returns True when ConnectProfile succeeded.

        ``profile-unavailable`` means BlueZ has no registered A2DP handler —
        WirePlumber's endpoint registration has been lost.  ``br-connection-unknown``
        is a distinct failure: endpoints are registered but BlueZ's internal
        transport negotiation failed (speaker SEPs appear then are immediately
        deleted).  Both are logged; the caller retries with backoff.
        """
        assert self._address is not None
        ok, msg = await self._run_subprocess(self._address, "connect")
        if ok:
            log.info("A2DP connection established to %s", self._address)
            return True
        if "profile-unavailable" in msg or "NotAvailable" in msg:
            log.warning(
                "A2DP connect rejected (profile unavailable) for %s"
                " — WirePlumber endpoints not registered; restart companion to recover",
                self._address,
            )
            return False
        log.warning("A2DP connect failed for %s: %s", self._address, msg)
        # A stale bond means BlueZ has no Device1 object at all — there is
        # nothing to disconnect, and introspecting the absent device would only
        # provoke a dbus_fast add-match ERROR.  Skip the cleanup in that case.
        # Match on the machine-readable status code, not the message wording.
        if error_code(msg) != STALE_BOND_CODE:
            await self._disconnect()
        return False

    async def _disconnect(self) -> None:
        """Disconnect from the device. No-op if already disconnected."""
        assert self._address is not None
        try:
            async with BluezClient() as bluez:
                await bluez.disconnect_a2dp(self._address)
        except Exception as exc:
            log.debug("A2DP disconnect for %s: %s", self._address, exc)
