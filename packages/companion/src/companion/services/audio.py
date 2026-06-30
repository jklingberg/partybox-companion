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
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TypeAlias

from companion.config import AudioSettings

log = logging.getLogger(__name__)

_CHECK_INTERVAL = 30.0  # seconds between health checks when connected
_CONNECT_TIMEOUT = 15.0  # seconds to wait for bluetoothctl connect
_RETRY_BASE = 10.0  # initial retry delay after a failed/lost connection
_RETRY_MAX = 300.0  # back off up to 5 minutes when another device is competing
_QUEUE_MAX = 64


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
                    await self._connect()
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, _RETRY_MAX)
                else:
                    if retry_delay > _RETRY_BASE:
                        log.info("A2DP connection stable, backoff reset")
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
