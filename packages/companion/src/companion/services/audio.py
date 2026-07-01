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
from dataclasses import dataclass
from typing import TypeAlias

from companion.config import AudioSettings
from companion.services.bluez_dbus import BluezClient

log = logging.getLogger(__name__)

_CHECK_INTERVAL = 60.0  # seconds between health checks when connected
_RETRY_BASE = 10.0  # initial retry delay after a failed/lost connection
_RETRY_MAX = 60.0  # cap backoff at 60 s — 5 min was too slow to recover
_QUEUE_MAX = 64

# WirePlumber health: after this many consecutive profile-unavailable failures
# (~15 min at max backoff) the A2DP endpoint registration has been lost and we
# restart WirePlumber to recover it. A cooldown prevents restart storms.
_WP_RESTART_THRESHOLD = 15
_WP_RESTART_COOLDOWN = 1200.0  # 20 minutes between automatic restarts


# ---------------------------------------------------------------------------
# Audio events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AudioReadyChanged:
    """Emitted when audio readiness transitions between ready and not ready.

    ``audio_ready`` is ``True`` when A2DP is connected and the appliance
    can produce audio; ``False`` when the A2DP link is absent.

    Subscribers receive this event on every transition.  Events are NOT
    emitted when the state is unchanged (e.g. if the connection check
    confirms an existing connection, no event fires).
    """

    audio_ready: bool


AudioServiceEvent: TypeAlias = AudioReadyChanged


class _AudioEventBus:
    """Broadcast dispatcher for AudioService events.

    Mirrors the EventBus pattern used by DeviceManager. Slow consumers
    have events dropped silently rather than stalling the emitter.
    """

    def __init__(self) -> None:
        self._queues: list[asyncio.Queue[AudioServiceEvent]] = []

    def subscribe(self) -> asyncio.Queue[AudioServiceEvent]:
        """Return a queue that receives all future events until unsubscribed."""
        q: asyncio.Queue[AudioServiceEvent] = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._queues.append(q)
        return q

    def unsubscribe(self, queue: asyncio.Queue[AudioServiceEvent]) -> None:
        """Stop delivering events to *queue*."""
        try:
            self._queues.remove(queue)
        except ValueError:
            pass

    def emit(self, event: AudioServiceEvent) -> None:
        """Broadcast *event* to all current subscribers, dropping for full queues."""
        for q in self._queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


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
        self._bus = _AudioEventBus()
        self._reconnect_now = asyncio.Event()
        self._profile_unavail_streak = 0
        self._last_wp_restart: float = 0.0
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
        q.put_nowait(AudioReadyChanged(audio_ready=self._audio_ready))
        return q

    def unsubscribe(self, queue: asyncio.Queue[AudioServiceEvent]) -> None:
        """Stop delivering events to *queue*."""
        self._bus.unsubscribe(queue)

    def update_address(self, address: str) -> None:
        """Set or update the A2DP sink address and interrupt any backoff sleep.

        Called by PairingService after a successful pairing.  When called while
        the service is sleeping between failed connect attempts, the sleep is
        cut short and a fresh attempt begins immediately (backoff reset).
        """
        self._address = address
        self._address_ready.set()
        self._reconnect_now.set()

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
                if not await self._is_connected():
                    self._set_audio_ready(False)
                    log.info(
                        "A2DP sink not connected, connecting to %s (retry in %.0fs)",
                        self._address,
                        retry_delay,
                    )
                    profile_unavail = await self._connect()
                    if profile_unavail:
                        self._profile_unavail_streak += 1
                        await self._maybe_restart_wireplumber()
                    else:
                        self._profile_unavail_streak = 0
                    # If _connect() succeeded, skip the backoff sleep and loop
                    # immediately so audio_ready=True is set before the speaker
                    # can drop the connection due to idle timeout.
                    if await self._is_connected():
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
                    self._profile_unavail_streak = 0
                    self._set_audio_ready(True)
                    retry_delay = _RETRY_BASE
                    await asyncio.sleep(_CHECK_INTERVAL)
        except asyncio.CancelledError:
            self._set_audio_ready(False)
            log.info("Audio service stopping")
            raise

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _set_audio_ready(self, val: bool) -> None:
        """Update audio_ready and emit AudioReadyChanged on any transition."""
        if val != self._audio_ready:
            self._audio_ready = val
            self._bus.emit(AudioReadyChanged(audio_ready=val))

    async def _is_connected(self) -> bool:
        assert self._address is not None
        ok, _ = await self._run_subprocess(self._address, "check")
        return ok

    @staticmethod
    async def _run_subprocess(address: str, command: str = "connect") -> tuple[bool, str]:
        """Run an A2DP helper command in a subprocess.

        bleak (used by DeviceManager) holds its own dbus-fast MessageBus in
        the same asyncio loop.  Any second MessageBus in the same loop
        misroutes D-Bus responses.  Running all BlueZ calls in isolated
        subprocesses sidesteps this entirely.

        command="connect" — returns (True, "") on success, (False, msg) on failure.
        command="check"   — returns (True, "") if connected, (False, "") if not.
        """
        import sys as _sys

        timeout = 35.0 if command == "connect" else 10.0
        proc = await asyncio.create_subprocess_exec(
            _sys.executable,
            "-m",
            "companion.services._a2dp_connect",
            address,
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
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
        """Attempt A2DP connection. Returns True when failure is profile-unavailable.

        A ``profile-unavailable`` result means BlueZ has no registered A2DP
        handler — WirePlumber's endpoint registration has been lost, not a
        speaker-side rejection.  The caller uses this signal to track how long
        the degraded state has lasted and trigger a WirePlumber restart when
        the streak exceeds :data:`_WP_RESTART_THRESHOLD`.
        """
        assert self._address is not None
        ok, msg = await self._run_subprocess(self._address, "connect")
        if ok:
            log.info("A2DP connection established to %s", self._address)
            return False
        if "profile-unavailable" in msg or "br-connection-unknown" in msg or "NotAvailable" in msg:
            log.warning(
                "A2DP connect rejected by speaker (profile unavailable) for %s",
                self._address,
            )
            return True
        log.warning("A2DP connect failed for %s: %s", self._address, msg)
        await self._disconnect()
        return False

    async def _maybe_restart_wireplumber(self) -> None:
        """Restart WirePlumber when A2DP endpoint loss is sustained.

        Called after every profile-unavailable failure.  No-ops until
        :data:`_WP_RESTART_THRESHOLD` consecutive failures have accumulated
        and :data:`_WP_RESTART_COOLDOWN` seconds have elapsed since the last
        automatic restart.  Sleeps 10 s after restarting to give WirePlumber
        time to re-register its A2DP endpoints with BlueZ.
        """
        if self._profile_unavail_streak < _WP_RESTART_THRESHOLD:
            return
        loop = asyncio.get_running_loop()
        if loop.time() - self._last_wp_restart < _WP_RESTART_COOLDOWN:
            return
        log.warning(
            "WirePlumber A2DP endpoints lost (%d consecutive failures); restarting WirePlumber",
            self._profile_unavail_streak,
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "sudo",
                "systemctl",
                "--user",
                "-M",
                "pi@",
                "restart",
                "wireplumber",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)
            if proc.returncode != 0:
                log.warning(
                    "WirePlumber restart failed (rc=%d): %s",
                    proc.returncode,
                    stderr.decode(errors="replace").strip(),
                )
            else:
                log.info("WirePlumber restarted; waiting for endpoint registration")
        except Exception as exc:
            log.warning("WirePlumber restart error: %s", exc)
        self._last_wp_restart = loop.time()
        self._profile_unavail_streak = 0
        await asyncio.sleep(10.0)

    async def _disconnect(self) -> None:
        """Disconnect from the device. No-op if already disconnected."""
        assert self._address is not None
        try:
            async with BluezClient() as bluez:
                await bluez.disconnect_a2dp(self._address)
        except Exception as exc:
            log.debug("A2DP disconnect for %s: %s", self._address, exc)
