"""librespot lifecycle manager — Spotify Connect for the appliance.

The SpotifyService owns the librespot subprocess. It starts librespot when
the appliance boots, monitors it for playback state changes, and restarts it
automatically after unexpected exits. On cancellation it terminates the
subprocess cleanly.

librespot is an implementation detail. The product is: this Pi appears as a
Spotify Connect speaker. Playback control (volume, skip, queue) stays inside
Spotify clients; the service only tracks whether librespot is running and
whether playback is currently active.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import re
import shutil
import signal
from dataclasses import dataclass

from companion.config import SpotifySettings
from companion.volume import VolumeState

log = logging.getLogger(__name__)

_RESTART_DELAY = 5.0
_NOT_FOUND_RETRY = 60.0

# librespot logs to stderr. These patterns detect playback state transitions.
# The matching is intentionally broad and case-insensitive — log format can
# vary between librespot releases. Best-effort is sufficient for a status display.
_ACTIVE_PATTERNS = ("is now playing", "loading track", "preloading")
_INACTIVE_PATTERNS = ("track paused", "track stopped", "end of track", "stopped")

# Matches librespot lines like: mixer: set volume to 65535 (100%)
_VOLUME_RE = re.compile(r"volume.*?\((\d+)%\)", re.IGNORECASE)


@dataclass(frozen=True)
class SpotifyStatus:
    """Point-in-time view of the Spotify Connect service."""

    running: bool
    active: bool
    device_name: str


class SpotifyService:
    """Manages the librespot subprocess for Spotify Connect.

    Instantiate and call :meth:`run` as an asyncio task::

        service = SpotifyService(settings.spotify)
        task = asyncio.create_task(service.run())
        # ... later ...
        task.cancel()
        await task

    While :meth:`run` is active, :attr:`status` reflects the current state.
    """

    def __init__(
        self,
        settings: SpotifySettings,
        volume_state: VolumeState | None = None,
    ) -> None:
        self._settings = settings
        self._volume_state = volume_state
        self._running = False
        self._active = False
        self._proc: asyncio.subprocess.Process | None = None

    @property
    def settings(self) -> SpotifySettings:
        """Current effective settings — may change after update_settings()."""
        return self._settings

    @property
    def status(self) -> SpotifyStatus:
        """Current point-in-time service state. Never blocks."""
        return SpotifyStatus(
            running=self._running,
            active=self._active,
            device_name=self._settings.connect_name,
        )

    def update_settings(self, settings: SpotifySettings) -> None:
        """Apply new settings and restart librespot.

        The current subprocess (if any) is terminated; the run() loop restarts
        it automatically with the new settings.
        """
        self._settings = settings
        log.info(
            "Spotify settings updated (device_name=%r, bitrate=%d) — restarting",
            settings.connect_name,
            settings.bitrate,
        )
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass

    async def run(self) -> None:
        """Start librespot and restart after unexpected exits. Runs until cancelled."""
        log.info("Spotify service starting (device_name=%r)", self._settings.connect_name)
        try:
            while True:
                await self._run_once()
        except asyncio.CancelledError:
            log.info("Spotify service stopping")
            await self._terminate()
            raise

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _run_once(self) -> None:
        if shutil.which("librespot") is None:
            log.warning(
                "librespot not found — install it to enable Spotify Connect (retrying in %.0fs)",
                _NOT_FOUND_RETRY,
            )
            self._running = False
            self._active = False
            await asyncio.sleep(_NOT_FOUND_RETRY)
            return

        cmd = self._build_command()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=self._preexec,
            )
        except OSError as exc:
            log.error("failed to start librespot: %s (retrying in %.0fs)", exc, _RESTART_DELAY)
            await asyncio.sleep(_RESTART_DELAY)
            return

        self._proc = proc
        self._running = True
        self._active = False
        log.info(
            "Spotify service started (pid=%d, device=%r)",
            proc.pid,
            self._settings.connect_name,
        )

        await self._monitor(proc)

        rc = proc.returncode
        self._proc = None
        self._running = False
        self._active = False

        if rc == 0:
            log.info("librespot exited cleanly")
        else:
            log.warning(
                "librespot exited (code=%s), restarting in %.0fs",
                rc,
                _RESTART_DELAY,
            )
            await asyncio.sleep(_RESTART_DELAY)

    async def _monitor(self, proc: asyncio.subprocess.Process) -> None:
        """Stream stderr until the process exits, inferring playback state."""
        stderr = proc.stderr
        if stderr is None:
            await proc.wait()
            return

        async for line_bytes in stderr:
            line = line_bytes.decode(errors="replace").rstrip()
            log.debug("librespot: %s", line)
            self._infer_playback_state(line)
            self._infer_volume(line)

        await proc.wait()

    def _infer_volume(self, line: str) -> None:
        if self._volume_state is None:
            return
        m = _VOLUME_RE.search(line)
        if m is not None:
            self._volume_state.update(int(m.group(1)))

    def _infer_playback_state(self, line: str) -> None:
        lower = line.lower()
        if any(p in lower for p in _ACTIVE_PATTERNS):
            if not self._active:
                self._active = True
                log.info("Spotify playback became active")
        elif any(p in lower for p in _INACTIVE_PATTERNS):
            if self._active:
                self._active = False
                log.info("Spotify playback stopped")

    async def _terminate(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except (TimeoutError, ProcessLookupError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        finally:
            self._proc = None
            self._running = False
            self._active = False

    @staticmethod
    def _preexec() -> None:
        # Ask the kernel to send SIGTERM to this child if the parent dies
        # unexpectedly (SIGKILL, hard crash). Without this, librespot becomes
        # an orphan and blocks Avahi re-registration on the next companion start.
        # PR_SET_PDEATHSIG = 1; Linux-only, safe to ignore on other platforms.
        try:
            ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, int(signal.SIGTERM), 0, 0, 0)
        except OSError:
            pass

    def _build_command(self) -> list[str]:
        cmd = [
            "librespot",
            "--name",
            self._settings.connect_name,
            "--bitrate",
            str(self._settings.bitrate),
            "--disable-audio-cache",
        ]
        if self._settings.backend is not None:
            cmd += ["--backend", self._settings.backend]
        return cmd
