"""Audio-focus watcher — detects competing Bluetooth sources on the speaker.

A2DP gives the source no feedback about rendering: when a second device
(typically a phone that auto-reconnected) is connected to the speaker, the
speaker can accept the companion's stream — transport ``active``, no AVDTP
error, ``audio_ready`` True — while rendering silence. Every Pi-side audio
signal looks healthy. The only known observable that reveals the competing
connection is the speaker's own FDDF LE advertisement, which carries a
connected-source indicator (see docs/reverse-engineering/protocol.md
§ "FDDF Advertisement" and ADR-027 for the FDDF discovery mechanism).

This service periodically runs a short LE scan (in a subprocess, for the
same bleak/dbus-fast isolation reasons as ``AudioService`` — see
``_a2dp_connect.py``) and classifies the freshest matching payload:

- ``exclusive`` — the companion is the only source connected to the speaker
- ``contested`` — at least one other source is connected; audio the companion
  sends may be silently discarded
- ``unknown`` — no speaker paired yet, or no fresh advertisement seen

The result is exposed on ``GET /api/v1/health`` (``audio_focus``) and pushed
to the Portal as ``audio_focus_changed`` WS events, where a ``contested``
state renders as a "disconnect the other device" warning.

Classification thresholds are model-observed on a PartyBox 520, not
specified — see ``bluez_dbus.parse_fddf_payload``. If a future model reports
a different idle baseline the rule errs toward ``exclusive`` (no warning)
rather than crying wolf.

**Scan cost — the radio is shared.** LE discovery competes with the A2DP
stream for the same controller's airtime: with audio flowing, a 12 s scan
window every 60 s produced clearly audible periodic stutter (2026-07-17
incident). The scan window is therefore a tight cap, and while the transport
is actively streaming (*streaming_fn*) the cadence stretches from
``_SCAN_INTERVAL`` to ``_STREAMING_SCAN_INTERVAL``. Scanning never stops
entirely during playback: "playing but rendering silence because another
source holds the speaker" is precisely the state this service exists to
expose, so contested detection must stay live — just cheaper.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from partyboxd.eventbus import EventBus

from companion.services.bluez_dbus import parse_fddf_payload

log = logging.getLogger(__name__)

_SCAN_INTERVAL = 60.0
_STREAMING_SCAN_INTERVAL = 120.0  # relaxed cadence while A2DP audio is flowing
# The helper exits early on the first matching advert (adverts arrive every
# ~1 s), so the window is a worst-case cap, not a fixed duration. The cap
# matters most while audio streams: A2DP starves advert reception, the scan
# runs its full window, and every scanned second is a second stolen from the
# stream — 12 s was audible as stutter (2026-07-17), 3 s still spans several
# advert intervals.
_SCAN_WINDOW = 3.0
_SCAN_SUBPROCESS_TIMEOUT = _SCAN_WINDOW + 10.0
_PAIRING_BACKOFF = 10.0  # re-check cadence while a pairing attempt owns discovery
_BLE_DOWN_RECHECK = 10.0  # re-check cadence while DeviceManager holds no connection
_MISS_LIMIT = 3  # consecutive scans without a fresh advert before UNKNOWN

# Idle baseline observed with only the companion connected (PartyBox 520).
_EXCLUSIVE_SOURCE_COUNT = 0x05
_EXCLUSIVE_CONNECTION_BITS = 0x01


class AudioFocus(StrEnum):
    EXCLUSIVE = "exclusive"
    CONTESTED = "contested"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AudioFocusChanged:
    """Emitted when the audio-focus classification transitions."""

    focus: str  # an AudioFocus value; str for direct WS JSON serialization
    type: Literal["audio_focus_changed"] = "audio_focus_changed"


type AudioFocusEvent = AudioFocusChanged

type ScanFn = Callable[[str], Awaitable[bytes | None]]

type StreamingFn = Callable[[], Awaitable[bool]]


class AudioFocusService:
    """Periodic FDDF-advert watcher classifying who holds the speaker's audio.

    *address_fn* returns the paired speaker's BR/EDR address (or ``None``
    before first pairing).  *pairing_active_fn* returns True while a pairing
    attempt is running — scans are skipped then, so this service's discovery
    traffic never competes with the pairing flow's own discovery windows.

    *streaming_fn* returns True while the A2DP transport is actively carrying
    audio (see ``AudioService.transport_active``); the loop then paces itself
    at *streaming_interval* instead of *interval* so scans steal as little
    radio time as possible from the live stream. ``None`` means "never
    streaming" (scan at *interval* always). The callable must not raise —
    collapse failures to False.

    *ble_connected_fn*, when provided, gates scanning on DeviceManager
    holding its control connection: scans are skipped entirely while it does
    not (``False``). This is not a cadence adjustment like *streaming_fn* —
    the scan is skipped outright, not just spaced out — because the case
    being avoided is scanning while DeviceManager is itself mid scan/connect
    on the same adapter. Investigation on 2026-07-18 correlated this
    service's scan windows with DeviceManager connect failures, including a
    live ADR-039 wedge (adapter power-cycle) triggered moments after one.
    The two scans are architecturally independent processes with no
    coordination otherwise, and a live-caught wedge is a stronger signal
    than any assumption about what BlueZ *should* handle gracefully.
    Skipping outright (vs. e.g. a scan/connect mutex keyed to
    DeviceManager's connect window) is the cheapest option for a
    resource-constrained Pi: no subprocess spawn, no D-Bus session, nothing
    at all, for as long as the condition holds — a deliberate trade against
    ``audio_focus`` freshness, since that signal is frozen at its last
    reading while paused instead of decaying to ``UNKNOWN``. Freshness is
    intentionally sacrificed for reconnect reliability: ``audio_focus`` is
    advisory UI information, BLE reconnection is operationally more
    important, and this is deliberate — don't "fix" the staleness by
    re-enabling scans here without re-litigating the trade-off above.
    ``None`` means "never gate" (matches older call sites / tests that
    predate this).

    *scan_fn* is injectable for tests; the default runs
    ``companion.services._fddf_scan`` in a subprocess.
    """

    def __init__(
        self,
        address_fn: Callable[[], str | None],
        pairing_active_fn: Callable[[], bool],
        *,
        scan_fn: ScanFn | None = None,
        interval: float = _SCAN_INTERVAL,
        streaming_fn: StreamingFn | None = None,
        streaming_interval: float = _STREAMING_SCAN_INTERVAL,
        ble_connected_fn: Callable[[], bool] | None = None,
    ) -> None:
        self._address_fn = address_fn
        self._pairing_active_fn = pairing_active_fn
        self._scan_fn: ScanFn = scan_fn if scan_fn is not None else _run_scan_subprocess
        self._interval = interval
        self._streaming_fn = streaming_fn
        self._streaming_interval = streaming_interval
        self._ble_connected_fn = ble_connected_fn
        self._focus = AudioFocus.UNKNOWN
        self._misses = 0
        self._bus: EventBus[AudioFocusEvent] = EventBus()

    @property
    def focus(self) -> AudioFocus:
        """Latest classification. UNKNOWN until a fresh advert has been seen."""
        return self._focus

    def subscribe(self) -> asyncio.Queue[AudioFocusEvent]:
        """Subscribe to focus transitions.

        Returns a queue pre-populated with the **current state** as its first
        event, matching the BehaviorSubject-style contract of the other
        companion services (see ``AudioService.subscribe``).
        """
        q = self._bus.subscribe()
        q.put_nowait(AudioFocusChanged(focus=self._focus))
        return q

    def unsubscribe(self, queue: asyncio.Queue[AudioFocusEvent]) -> None:
        """Stop delivering events to *queue*."""
        self._bus.unsubscribe(queue)

    async def run(self) -> None:
        """Scan-classify loop. Runs until cancelled."""
        while True:
            address = self._address_fn()
            if address is None:
                self._set_focus(AudioFocus.UNKNOWN)
                await asyncio.sleep(self._interval)
                continue
            if self._pairing_active_fn():
                await asyncio.sleep(_PAIRING_BACKOFF)
                continue
            if self._ble_connected_fn is not None and not self._ble_connected_fn():
                # Skip the scan outright — see ble_connected_fn in the class
                # docstring. focus is deliberately left untouched (frozen at
                # its last reading) rather than decayed toward UNKNOWN.
                await asyncio.sleep(_BLE_DOWN_RECHECK)
                continue

            raw = await self._scan_fn(address)
            if raw is None:
                self._misses += 1
                # The speaker advertises FDDF continuously while powered, so
                # repeated misses mean it is off/out of range — the answer is
                # genuinely unknown, not "keep showing the last state".
                if self._misses >= _MISS_LIMIT:
                    self._set_focus(AudioFocus.UNKNOWN)
            else:
                self._misses = 0
                payload = parse_fddf_payload(raw)
                if payload is None:
                    log.debug("audio focus: unparseable FDDF payload %s", raw.hex())
                    self._set_focus(AudioFocus.UNKNOWN)
                else:
                    contested = (
                        payload.source_count > _EXCLUSIVE_SOURCE_COUNT
                        or payload.connection_bits & ~_EXCLUSIVE_CONNECTION_BITS
                    )
                    focus = AudioFocus.CONTESTED if contested else AudioFocus.EXCLUSIVE
                    if focus != self._focus:
                        log.info(
                            "audio focus: %s -> %s (source_count=%#04x connection_bits=%#04x"
                            " payload=%s)",
                            self._focus.value,
                            focus.value,
                            payload.source_count,
                            payload.connection_bits,
                            raw.hex(),
                        )
                    self._set_focus(focus)
            await asyncio.sleep(await self._next_interval())

    async def _next_interval(self) -> float:
        """Cadence for the next cycle: relaxed while audio is streaming."""
        if self._streaming_fn is not None and await self._streaming_fn():
            return self._streaming_interval
        return self._interval

    def _set_focus(self, focus: AudioFocus) -> None:
        if focus == self._focus:
            return
        self._focus = focus
        self._bus.emit(AudioFocusChanged(focus=focus))


async def _run_scan_subprocess(address: str) -> bytes | None:
    """Run one ``_fddf_scan`` window; return the payload or None.

    All failure shapes (helper error line, timeout, spawn failure) collapse to
    None — the caller's miss counter, not this function, decides when repeated
    failure becomes UNKNOWN.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "companion.services._fddf_scan",
            address,
            str(_SCAN_WINDOW),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        log.debug("audio focus: scan subprocess spawn failed: %s", exc)
        return None
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_SCAN_SUBPROCESS_TIMEOUT
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        log.debug("audio focus: scan subprocess timed out")
        return None
    line = stdout.decode(errors="replace").strip()
    if line.startswith("hex:"):
        try:
            return bytes.fromhex(line[len("hex:") :])
        except ValueError:
            log.debug("audio focus: malformed scan output %r", line)
            return None
    if line.startswith("err:"):
        log.debug(
            "audio focus: scan failed: %s (stderr: %s)",
            line,
            stderr.decode(errors="replace").strip(),
        )
    return None
