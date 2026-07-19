"""librespot lifecycle manager — Spotify Connect for the appliance.

The SpotifyService owns the librespot subprocess. It starts librespot when
the appliance boots, monitors it for playback state changes, and restarts it
automatically after unexpected exits. On cancellation it terminates the
subprocess cleanly.

librespot is an implementation detail. The product is: this Pi appears as a
Spotify Connect speaker. Playback control (volume, skip, queue) stays inside
Spotify clients; the service only tracks whether librespot is running and its
current playback state (stopped/playing/paused).

Playback state comes from librespot's own event hook (``--onevent``), not
from log scraping: librespot 0.8's stderr is silent across pause/resume (only
track-load lines are logged at default verbosity), so a log-line heuristic
can detect "started playing" but can never detect "paused" — see the M18
validation notes this replaced. ``--onevent`` runs a program (see
``_ensure_runtime_files``) for every playback event, including "paused",
with the event name in the ``PLAYER_EVENT`` env var. That program
(``_librespot_onevent.py``) forwards the event over a Unix domain socket this
service listens on — see ``_handle_event_connection``.

Event flow, librespot's playback event to a client watching the Portal::

    librespot
        │  --onevent <script>, PLAYER_EVENT=playing|paused|stopped|... in env
        ▼
    generated shell shim (_ensure_runtime_files, runtime_dir/librespot-onevent.sh)
        │  exec python -m companion.services._librespot_onevent
        ▼
    _librespot_onevent.py  (short-lived subprocess, one per event)
        │  connects + writes PLAYER_EVENT to COMPANION_SPOTIFY_EVENT_SOCK
        ▼
    Unix domain socket  (runtime_dir/spotify-events.sock)
        │  accepted by _handle_event_connection
        ▼
    SpotifyService._apply_player_event  →  self._state, self._running
        │  _set_status() emits SpotifyStatusChanged on any change
        ▼
    EventBus  (self._bus, see subscribe()/unsubscribe())
        │
        ▼
    REST (GET /api/v1/spotify) · WS (`spotify_changed`) · Portal UI
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import re
import shlex
import shutil
import signal
import sys
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from partyboxd.eventbus import EventBus

from companion.config import SpotifySettings
from companion.volume import VolumeState

log = logging.getLogger(__name__)

_RESTART_DELAY = 5.0
_NOT_FOUND_RETRY = 60.0
_EVENT_READ_TIMEOUT = 2.0
_DEFAULT_RUNTIME_DIR = Path(tempfile.gettempdir()) / "companion"

type PlaybackState = Literal["stopped", "playing", "paused"]

# librespot --onevent PLAYER_EVENT values that map to a playback state change.
# Other events it emits ("changed", "loading", "preloading", "seeked",
# "volume_set", "shuffle_changed", ...) don't affect play/pause and are
# ignored. "session_disconnected" resets to stopped — the Spotify client that
# was driving playback went away.
_PLAYER_EVENT_TO_STATE: dict[str, PlaybackState] = {
    "playing": "playing",
    "paused": "paused",
    "stopped": "stopped",
    "session_disconnected": "stopped",
}

# librespot line prefix, e.g. "[2026-07-03T14:15:55Z WARN  librespot_connect::spirc]".
# Used to surface librespot's own WARN/ERROR lines at a visible log level —
# an incident where playback died with the cause logged only at DEBUG
# motivated this (M18 validation run, RC13).
_LIBRESPOT_LEVEL_RE = re.compile(r"^\[\S+ (WARN|ERROR)\s")

# Matches librespot lines like: mixer: set volume to 65535 (100%)
_VOLUME_RE = re.compile(r"volume.*?\((\d+)%\)", re.IGNORECASE)


@dataclass(frozen=True)
class SpotifyStatus:
    """Point-in-time view of the Spotify Connect service."""

    running: bool
    state: PlaybackState
    device_name: str


@dataclass(frozen=True)
class SpotifyStatusChanged:
    """Emitted when running or state transitions.

    Lets WS subscribers (the Portal, via the merged event stream — see
    docs/adr/035-state-ownership-and-signal-pipeline.md) update their view
    of ``GET /api/v1/spotify`` without polling for it.
    """

    running: bool
    state: PlaybackState
    device_name: str
    type: Literal["spotify_changed"] = "spotify_changed"


type SpotifyEvent = SpotifyStatusChanged


class SpotifyService:
    """Manages the librespot subprocess for Spotify Connect.

    Instantiate and call :meth:`run` as an asyncio task::

        service = SpotifyService(settings.spotify)
        task = asyncio.create_task(service.run())
        # ... later ...
        task.cancel()
        await task

    While :meth:`run` is active, :attr:`status` reflects the current state.
    Subscribers receive :class:`SpotifyStatusChanged` events on every
    running/state transition; see :meth:`subscribe`.
    """

    def __init__(
        self,
        settings: SpotifySettings,
        volume_state: VolumeState | None = None,
        runtime_dir: Path = _DEFAULT_RUNTIME_DIR,
    ) -> None:
        self._settings = settings
        self._volume_state = volume_state
        self._runtime_dir = runtime_dir
        self._onevent_script_path = runtime_dir / "librespot-onevent.sh"
        self._event_sock_path = runtime_dir / "spotify-events.sock"
        self._running = False
        self._state: PlaybackState = "stopped"
        self._proc: asyncio.subprocess.Process | None = None
        self._bus: EventBus[SpotifyEvent] = EventBus()

    @property
    def settings(self) -> SpotifySettings:
        """Current effective settings — may change after update_settings()."""
        return self._settings

    def subscribe(self) -> asyncio.Queue[SpotifyEvent]:
        """Subscribe to running/state changes.

        Returns a queue pre-populated with the **current state** as its
        first event, followed by :class:`SpotifyStatusChanged` events for
        all future transitions — callers never need to read :attr:`status`
        separately.

        Call :meth:`unsubscribe` with the returned queue when done.
        """
        q = self._bus.subscribe()
        q.put_nowait(
            SpotifyStatusChanged(
                running=self._running, state=self._state, device_name=self._settings.connect_name
            )
        )
        return q

    def unsubscribe(self, queue: asyncio.Queue[SpotifyEvent]) -> None:
        """Stop delivering events to *queue*."""
        self._bus.unsubscribe(queue)

    @property
    def status(self) -> SpotifyStatus:
        """Current point-in-time service state. Never blocks."""
        return SpotifyStatus(
            running=self._running,
            state=self._state,
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
        event_server = await self._start_event_server()
        try:
            while True:
                await self._run_once()
        except asyncio.CancelledError:
            log.info("Spotify service stopping")
            await self._terminate()
            raise
        finally:
            event_server.close()
            await event_server.wait_closed()
            self._event_sock_path.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _start_event_server(self) -> asyncio.Server:
        """Create the runtime dir/launcher script and bind the --onevent socket.

        Retries on OSError (e.g. the runtime dir isn't writable yet at early
        boot) instead of raising. This matters here specifically:
        SpotifyService.run() is launched via a bare ``asyncio.create_task()``
        inside ``_gate_spotify_on_audio`` (see ``__main__.py``), not awaited
        by the Supervisor directly — an uncaught exception here would be
        silently dropped (surfacing only as an unretrieved-task-exception
        warning) rather than triggering a restart. Retrying keeps
        SpotifyService self-healing on its own, per its own docstring
        ("owns subprocess crash-recovery internally"), without depending on
        that propagation path.

        Removing any stale socket file before binding also covers the crash
        case: if a prior companion process died without reaching the
        `finally` cleanup below, `start_unix_server` would otherwise fail
        with "address already in use" against the leftover socket file.
        """
        attempt = 0
        while True:
            try:
                self._ensure_runtime_files()
                self._event_sock_path.unlink(missing_ok=True)
                server = await asyncio.start_unix_server(
                    self._handle_event_connection, path=str(self._event_sock_path)
                )
            except OSError as exc:
                attempt += 1
                log.error(
                    "failed to set up Spotify event socket in %s: %s (retrying in %.0fs)",
                    self._runtime_dir,
                    exc,
                    _RESTART_DELAY,
                )
                await asyncio.sleep(_RESTART_DELAY)
            else:
                if attempt:
                    # Only log recovery if setup didn't succeed on the first try —
                    # otherwise every normal start would log this alongside the
                    # existing "Spotify service starting" line for no added value.
                    log.info(
                        "Spotify event socket initialized in %s after %d failed attempt(s)",
                        self._runtime_dir,
                        attempt,
                    )
                return server

    def _ensure_runtime_files(self) -> None:
        """Create the runtime dir and the librespot --onevent launcher script.

        librespot's ``--onevent`` takes a single executable path — it cannot
        invoke ``python -m ...`` directly — so this writes a tiny shell shim
        that execs the real hook (:mod:`companion.services._librespot_onevent`)
        via the current interpreter. Regenerated on every start so a Python
        interpreter path change (e.g. venv rebuild) doesn't leave a stale shim.

        The runtime dir is chmod'd 0700: it holds the event socket and this
        executable script, and should not be readable by other local users.
        On the appliance this is already true (RuntimeDirectoryMode=0700 in
        companion.service), but ``mkdir(exist_ok=True)`` does not update the
        mode of a pre-existing directory, and the dev-mode default (a temp
        dir) has no such guarantee — so it's set explicitly here regardless.

        The script itself is written to a temp file and atomically renamed
        into place (same filesystem, since both live in runtime_dir) so a
        process interrupted mid-write never leaves librespot pointing at a
        truncated or half-written launcher.
        """
        if self._runtime_dir.exists() and not self._runtime_dir.is_dir():
            # mkdir(exist_ok=True) would raise FileExistsError here anyway, but
            # with a message that doesn't say *why* — spelling it out saves
            # whoever's debugging a confusing misconfiguration (e.g. runtime_dir
            # pointed at a file left over from something else) a trip to a
            # traceback to figure out what's actually wrong.
            raise NotADirectoryError(
                f"Spotify runtime_dir {self._runtime_dir} exists and is not a directory"
            )
        self._runtime_dir.mkdir(parents=True, exist_ok=True)
        self._runtime_dir.chmod(0o700)
        script = (
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} -m companion.services._librespot_onevent\n"
        )
        tmp_path = self._onevent_script_path.with_name(self._onevent_script_path.name + ".tmp")
        tmp_path.write_text(script)
        tmp_path.chmod(0o755)
        tmp_path.replace(self._onevent_script_path)

    async def _handle_event_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle one connection from the librespot onevent hook (see module docstring)."""
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=_EVENT_READ_TIMEOUT)
            self._apply_player_event(line.decode(errors="replace").strip())
        except TimeoutError:
            pass
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    def _apply_player_event(self, event: str) -> None:
        state = _PLAYER_EVENT_TO_STATE.get(event)
        if state is None:
            log.debug("Spotify: ignoring unknown PLAYER_EVENT %r", event)
            return
        self._set_status(running=self._running, state=state)

    async def _run_once(self) -> None:
        if shutil.which("librespot") is None:
            log.warning(
                "librespot not found — install it to enable Spotify Connect (retrying in %.0fs)",
                _NOT_FOUND_RETRY,
            )
            self._set_status(running=False, state="stopped")
            await asyncio.sleep(_NOT_FOUND_RETRY)
            return

        cmd = self._build_command()
        env = {**os.environ, "COMPANION_SPOTIFY_EVENT_SOCK": str(self._event_sock_path)}
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=self._preexec,
                env=env,
            )
        except OSError as exc:
            log.error("failed to start librespot: %s (retrying in %.0fs)", exc, _RESTART_DELAY)
            await asyncio.sleep(_RESTART_DELAY)
            return

        self._proc = proc
        self._set_status(running=True, state="stopped")
        log.info(
            "Spotify service started (pid=%d, device=%r)",
            proc.pid,
            self._settings.connect_name,
        )

        await self._monitor(proc)

        rc = proc.returncode
        self._proc = None
        self._set_status(running=False, state="stopped")

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
        """Stream stderr until the process exits.

        Playback state arrives separately via the --onevent socket (see
        _handle_event_connection); this loop only surfaces librespot's own
        log lines and infers the output volume.
        """
        stderr = proc.stderr
        if stderr is None:
            await proc.wait()
            return

        async for line_bytes in stderr:
            line = line_bytes.decode(errors="replace").rstrip()
            m = _LIBRESPOT_LEVEL_RE.match(line)
            if m is None:
                log.debug("librespot: %s", line)
            elif m.group(1) == "ERROR":
                log.error("librespot: %s", line)
            else:
                log.warning("librespot: %s", line)
            self._infer_volume(line)

        await proc.wait()

    def _infer_volume(self, line: str) -> None:
        if self._volume_state is None:
            return
        m = _VOLUME_RE.search(line)
        if m is not None:
            self._volume_state.update(int(m.group(1)), "spotify")

    async def _terminate(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError, ProcessLookupError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        finally:
            self._proc = None
            self._set_status(running=False, state="stopped")

    def _set_status(self, *, running: bool, state: PlaybackState) -> None:
        """Update running/state and emit SpotifyStatusChanged if either changed."""
        if running == self._running and state == self._state:
            return
        self._running = running
        self._state = state
        self._bus.emit(
            SpotifyStatusChanged(
                running=running, state=state, device_name=self._settings.connect_name
            )
        )

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
            "--onevent",
            str(self._onevent_script_path),
            "--emit-sink-events",
        ]
        if self._settings.backend is not None:
            cmd += ["--backend", self._settings.backend]
        return cmd
