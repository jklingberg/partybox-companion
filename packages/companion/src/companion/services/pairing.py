"""Bluetooth Classic A2DP pairing service.

Manages first-time pairing between the Pi and the speaker.  On a fresh
appliance image there is no stored A2DP address, so AudioService has no sink
to connect to.  PairingService handles the one-shot flow:

1. Make the adapter bondable for the duration of this attempt only.
2. Discover the speaker's BR/EDR address from its LE advertisement (Harman
   FDDF service data) and pair as soon as it is found.
3. Trust and connect the BR/EDR link.
4. Persist the address to PortalConfig so AudioService survives reboots.
5. Hand the address to AudioService so it starts without a process restart.

See docs/adr/027-bluetooth-bonding-architecture.md for why discovery and
pairing are a single event-driven transition rather than a fixed-duration
scan followed by a separate pair step: the JBL's BR/EDR pairing window is
short, so this service must not spend it on scanning.

Only one pairing attempt can run at a time.  Calling :meth:`start` while
a pairing is already in progress is a no-op that returns ``False``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum

from companion.config_store import ConfigStore
from companion.services.audio import AudioService
from companion.services.bluez_dbus import BluezClient, PairingFailedError

log = logging.getLogger(__name__)

# Outer give-up timeout for the whole discover-and-pair attempt. Not a scan
# duration that gets fully consumed before pairing is attempted — discovery
# resolves and triggers pairing immediately on the first match.
_DISCOVERY_TIMEOUT = 60.0


class PairingState(StrEnum):
    IDLE = "idle"
    SCANNING = "scanning"
    PAIRING = "pairing"
    FAILED = "failed"


@dataclass(frozen=True)
class PairingStatus:
    state: PairingState
    error: str | None = None


class PairingService:
    """On-demand Bluetooth Classic pairing for the A2DP audio sink.

    This is not a long-running Supervisor service.  It is an on-demand helper
    invoked by the ``POST /api/v1/audio/pair`` endpoint.  Internally it
    manages a single asyncio Task so callers do not block waiting for the
    discovery/pairing window.
    """

    def __init__(self, config: ConfigStore, audio: AudioService) -> None:
        self._config = config
        self._audio = audio
        self._state = PairingState.IDLE
        self._error: str | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def status(self) -> PairingStatus:
        return PairingStatus(state=self._state, error=self._error)

    async def start(self) -> bool:
        """Begin a pairing attempt in the background.

        Returns ``True`` if pairing started, ``False`` if already in progress.
        The caller should poll :attr:`status` or ``GET /api/v1/audio`` to
        track progress.
        """
        if self._task is not None and not self._task.done():
            return False
        self._error = None
        self._task = asyncio.create_task(self._do_pair(), name="pairing")
        return True

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _do_pair(self) -> None:
        self._state = PairingState.SCANNING
        try:
            async with BluezClient() as bluez, bluez.pairing_agent():
                # Bondable mode is scoped to this single attempt (ADR-027
                # decision 2) — Just Works/NoInputNoOutput means a
                # permanently bondable adapter would accept silent bonds
                # from any nearby device.
                await bluez.set_pairable(True)
                try:
                    log.info("Pairing: discovering speaker (%.0fs window)", _DISCOVERY_TIMEOUT)
                    mac = await bluez.discover_bredr_address(timeout=_DISCOVERY_TIMEOUT)
                    if mac is None:
                        self._state = PairingState.FAILED
                        self._error = (
                            "Speaker not found. Put the speaker in pairing mode and try again."
                        )
                        log.warning("Pairing: no JBL device found")
                        return

                    log.info("Pairing: found speaker at %s — pairing immediately", mac)
                    self._state = PairingState.PAIRING

                    try:
                        await bluez.pair(mac)
                    except PairingFailedError as exc:
                        self._state = PairingState.FAILED
                        self._error = (
                            "Pairing failed. Put the speaker in pairing mode "
                            "(press the Bluetooth button once until the LEDs flash) "
                            "and try again."
                        )
                        log.warning("Pairing: %s", exc)
                        return

                    await bluez.trust(mac)
                    await bluez.connect(mac)

                    # Persist so AudioService survives reboots.
                    cfg = self._config.read()
                    self._config.write(cfg.model_copy(update={"audio_sink_address": mac}))
                    log.info("Pairing: address %s saved to config", mac)

                    # Wake AudioService immediately without a process restart.
                    self._audio.update_address(mac)

                    self._state = PairingState.IDLE
                    log.info("Pairing: complete")
                finally:
                    await bluez.set_pairable(False)
        except asyncio.CancelledError:
            self._state = PairingState.IDLE
            raise
        except Exception as exc:
            self._state = PairingState.FAILED
            self._error = f"Unexpected error: {exc}"
            log.error("Pairing: unexpected error: %s", exc, exc_info=True)
