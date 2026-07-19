"""partybox-companion appliance entry point.

Starts the DeviceManager, the partyboxd HTTP API, the Companion Portal, and
the Spotify Connect service (librespot) as a single process. Both the daemon
and the Portal are served on the same port — the Portal at ``/`` and the
REST API at ``/api/v1/*``.

Usage::

    partybox-companion                                  # 0.0.0.0:8080
    COMPANION_PORT=80 partybox-companion                # bind to port 80
    COMPANION_SPOTIFY__CONNECT_NAME="Living Room" partybox-companion
    COMPANION_LOG_LEVEL=DEBUG partybox-companion        # verbose logging
"""

from __future__ import annotations

import asyncio
import logging
import logging.config
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress

import uvicorn
from fastapi import FastAPI
from partyboxd.api import create_app as create_daemon_app
from partyboxd.api.auth import make_auth_dependency
from partyboxd.config import Settings as DaemonSettings
from partyboxd.device import DeviceManager
from partyboxd.device.events import SpeakerStateChangedEvent, VolumeChangedEvent

from companion.config import AudioSettings, CompanionSettings, SpotifySettings
from companion.config_store import ConfigStore
from companion.services import login1_dbus
from companion.services.adapter_recovery import reset_adapter
from companion.services.audio import AudioService
from companion.services.audio_focus import AudioFocusService
from companion.services.le_reclaim import disconnect_stale_speaker_links
from companion.services.pairing import PairingService, PairingState
from companion.services.provisioning import ProvisioningService
from companion.services.router import make_services_router
from companion.services.spotify import SpotifyService
from companion.supervisor import RestartPolicy, Supervisor
from companion.volume import VolumeState
from companion.webui.router import make_portal_router
from companion.wifi.middleware import CaptivePortalMiddleware
from companion.wifi.router import make_wifi_router

log = logging.getLogger(__name__)


def _make_log_config(level: str) -> dict[str, object]:
    # When stdout is connected to journald, it adds timestamps and priority.
    # Drop those from the Python format to avoid duplication.
    fmt = (
        "%(levelname)-8s %(name)s %(message)s"
        if "JOURNAL_STREAM" in os.environ
        else "%(asctime)s %(levelname)-8s %(name)s %(message)s"
    )
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": fmt,
                "datefmt": "%Y-%m-%dT%H:%M:%S",
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "default",
            }
        },
        "root": {"level": level, "handlers": ["console"]},
        "loggers": {
            "uvicorn.access": {"level": "WARNING"},
        },
    }


_AUDIO_GRACE_SECONDS = 300.0


async def _gate_spotify_on_audio(audio: AudioService, spotify: SpotifyService) -> None:
    """Gate SpotifyService on audio readiness.

    librespot advertises a Spotify Connect device over Zeroconf.  If it runs
    while A2DP is absent, Spotify clients can select "PartyBox" and receive
    silence.  This gate ensures Spotify is visible only when the
    appliance can actually produce audio.

    Subscribes to AudioService events; the BehaviorSubject-style subscribe()
    delivers the current state immediately, so the gate reaches the correct
    initial state without waiting for the next audio transition.

    Start: Spotify starts as soon as audio becomes ready.
    Stop: Spotify stops _AUDIO_GRACE_SECONDS after audio goes away, giving the
    A2DP link time to recover from transient drops before removing the Spotify
    Connect device from clients' device lists.

    SpotifyService.run() owns subprocess crash-recovery internally; this gate
    does not supervise it.  See ADR-026.

    KNOWN GAP (github.com/jklingberg/partybox-companion/issues/65):
    spotify_task below is a bare asyncio.create_task() and is only ever
    awaited on the grace-period-timeout and cancellation paths — not on the
    "audio stays ready" path, which is the common case.  An uncaught
    exception from spotify.run() while audio is ready is therefore never
    retrieved here, this coroutine keeps running as if nothing happened, and
    the Supervisor (which only sees "spotify-audio-gate", not spotify.run()
    itself) never restarts anything.  Despite what an earlier version of
    this docstring claimed, an exception here does NOT currently propagate
    to the Supervisor.  Fix tracked in the issue above.
    """
    queue = audio.subscribe()
    spotify_task: asyncio.Task[None] | None = None

    try:
        while True:
            event = await queue.get()

            if event.audio_ready:
                if spotify_task is None:
                    log.info("audio gate: audio ready — starting Spotify Connect")
                    spotify_task = asyncio.create_task(spotify.run(), name="spotify-service")
            else:
                if spotify_task is not None:
                    log.info(
                        "audio gate: audio unavailable — %.0fs grace before stopping Spotify",
                        _AUDIO_GRACE_SECONDS,
                    )
                    try:
                        recovery = await asyncio.wait_for(queue.get(), timeout=_AUDIO_GRACE_SECONDS)
                    except TimeoutError:
                        log.info("audio gate: grace period expired — stopping Spotify Connect")
                        spotify_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await spotify_task
                        spotify_task = None
                    else:
                        if recovery.audio_ready:
                            log.info("audio gate: audio restored within grace period")
    finally:
        audio.unsubscribe(queue)
        if spotify_task is not None:
            spotify_task.cancel()
            with suppress(asyncio.CancelledError):
                await spotify_task


async def _forward_ble_volume(manager: DeviceManager, volume_state: VolumeState) -> None:
    """Forward VolumeChangedEvents from the device bus into VolumeState.

    Subscribes to DeviceManager's event bus and updates VolumeState whenever
    the hardware reports a volume change.  Runs until cancelled.

    While BLE volume is not yet implemented this coroutine subscribes but
    never receives a VolumeChangedEvent.  It exists to establish the wiring
    before BLE notifications arrive; see ADR-022.
    """
    queue = manager.subscribe()
    try:
        while True:
            event = await queue.get()
            if isinstance(event, VolumeChangedEvent):
                volume_state.update(event.percent, "ble")
    finally:
        manager.unsubscribe(queue)


async def _recheck_audio_on_standby(manager: DeviceManager, audio: AudioService) -> None:
    """Keep AudioService in step with the speaker's power state.

    The BLE control link and the A2DP audio link are separate Bluetooth
    subsystems (see ``AudioService``'s module docstring), but in practice
    they track the same speaker, so its power transitions matter in both
    directions:

    - Leaving "on": the audio link almost certainly just dropped too, so
      nudge a re-check (``recheck_now``) instead of letting the Portal show
      a stale "connected" for up to AudioService's 60s idle interval.
    - Entering "on": the speaker just woke up, so an A2DP connect is now
      likely to succeed — fire ``retry_now`` to break out of any failure
      back-off or cool-down accumulated while it was asleep. Without this,
      powering the speaker on from the Portal right after a failure run
      left it silent for up to the full 300s cool-down (observed
      2026-07-18).

    Runs until cancelled.
    """
    queue = manager.subscribe()
    try:
        while True:
            event = await queue.get()
            if isinstance(event, SpeakerStateChangedEvent):
                if event.state == "on":
                    audio.retry_now("speaker woke up")
                else:
                    audio.recheck_now()
    finally:
        manager.unsubscribe(queue)


_IDLE_SHUTDOWN_CHECK_INTERVAL = 15.0  # matches ADR-033's health-check cadence

#: Fixed thresholds, not Portal-configurable (ADR-038). Real-world timing is
#: dominated by however long the PartyBox's own undocumented on -> standby
#: -> off progression takes, which Companion doesn't control — a
#: user-tunable number here mostly wasn't doing useful work, only adding UI
#: surface for a value nobody had a principled way to choose. Always on;
#: there is no disable switch.
#:
#: "standby" is recoverable without touching the speaker (resuming playback
#: wakes the amp back up on its own, per observed PartyBox behaviour), so it
#: gets a generous allowance — long enough that an ordinary pause between
#: songs, or a quiet moment at a party, doesn't take the whole appliance
#: down. 30 min is the upper end of the "15-30 minutes" range the product
#: goal was originally framed in (ADR-038) — picked as the single fixed
#: value on the more conservative/generous side once the threshold stopped
#: being Portal-configurable, rather than re-derived from anything
#: measured. No hardware signal ties it to 30 specifically; it is a product
#: choice, not an engineering one.
_STANDBY_GRACE_SECONDS = 30 * 60

#: "off" means the BLE radio itself has gone dark — nothing remote can
#: recover that regardless of whether the Pi keeps running, so it only needs
#: enough of a debounce to rule out a transient reconnect blip (observed
#: clearing on its own within under a minute on real hardware), not a full
#: "maybe they're just pausing" grace period.
_OFF_STATE_GRACE_SECONDS = 90.0


async def _idle_battery_shutdown(
    manager: DeviceManager,
    power_off: Callable[[], Awaitable[None]] = login1_dbus.power_off,
) -> None:
    """Power off the Pi after sustained idle-on-battery time (ADR-038).

    Polls rather than reacts to events: ``SpeakerStateChangedEvent`` only
    fires on off/standby/on transitions, not on charging-status changes
    (e.g. the user plugs in mains while the speaker stays in standby), so a
    periodic re-check of the full snapshot is needed to notice that too.

    Counts idle time in ``"standby"`` (BLE connected, speaker asleep),
    ``"unreachable"`` (BLE disconnected, but the speaker's FDDF beacon
    proves it's still powered — see ``StatusSnapshot.beacon_seen``), and
    ``"off"`` (BLE disconnected, nothing seen at all), judged against
    *different* fixed thresholds — they are not equivalent, see
    ``_STANDBY_GRACE_SECONDS``/``_OFF_STATE_GRACE_SECONDS`` above.
    ``"unreachable"`` uses the same generous threshold as ``"standby"``:
    the beacon is exactly the confirmation that would otherwise be missing
    — the speaker is still drawing power, only its control channel is
    unreachable, so there is no reason to judge it more urgently than a
    speaker we know is merely asleep.

    The idle clock itself is a single continuous counter that survives the
    standby <-> off transition — only the threshold it's compared against
    changes with the current state. A speaker idle a long time in standby
    that then drops to "off" fires almost immediately, since the total idle
    time already dwarfs the short off-state threshold; there is no reason to
    make it wait out an *additional* grace period on top of idle time it has
    already accrued.

    "off" also means the current power source can no longer be
    reconfirmed, so both branches rely on ``last_known_on_battery``: the
    most recent confirmed ``charging_status`` reading, frozen at whatever it
    was when last seen. This is a deliberate accepted trade-off, not an
    oversight (ADR-038's hardware-validation follow-up): a speaker that goes
    out of range or gets moved to mains *during* an idle spell is still
    judged by the stale reading until it reconnects. Real activity
    (``"on"``) or a confirmed mains reading always resets the idle clock.
    Runs until cancelled.
    """
    idle_since: float | None = None
    triggered = False
    last_known_on_battery: bool | None = None
    while True:
        await asyncio.sleep(_IDLE_SHUTDOWN_CHECK_INTERVAL)
        if triggered:
            continue
        snap = manager.snapshot
        charging_status = snap.battery_status.charging_status if snap.battery_status else None
        if charging_status is not None:
            last_known_on_battery = not charging_status.on_mains

        threshold: float | None = None
        if snap.speaker_state in ("standby", "unreachable"):
            threshold = _STANDBY_GRACE_SECONDS
        elif snap.speaker_state == "off":
            threshold = _OFF_STATE_GRACE_SECONDS

        if threshold is not None and last_known_on_battery:
            if idle_since is None:
                idle_since = time.monotonic()
            elif time.monotonic() - idle_since >= threshold:
                log.warning(
                    "idle battery shutdown: no activity for %.0fs on (confirmed or "
                    "last-known) battery power — powering off",
                    time.monotonic() - idle_since,
                )
                triggered = True
                await power_off()
        else:
            idle_since = None


def main() -> None:
    level = os.environ.get("COMPANION_LOG_LEVEL", "INFO").upper()
    logging.config.dictConfig(_make_log_config(level))
    daemon_settings = DaemonSettings()
    companion_settings = CompanionSettings()
    asyncio.run(_run(daemon_settings, companion_settings))


async def _run(
    daemon_settings: DaemonSettings,
    companion_settings: CompanionSettings,
) -> None:
    # Single ConfigStore shared across all routers — one file handle, one source of truth.
    config_store = ConfigStore(companion_settings.data_dir / "config.json")

    # Load portal config so user-saved settings (Spotify Connect name, bitrate)
    # survive reboots. The config file may not exist on first boot — defaults
    # are used.
    portal_cfg = config_store.read()
    effective_spotify = SpotifySettings(
        connect_name=portal_cfg.spotify_connect_name,
        bitrate=portal_cfg.spotify_bitrate,
        backend=companion_settings.spotify.backend,
    )
    volume_state = VolumeState()
    spotify = SpotifyService(
        effective_spotify, volume_state=volume_state, runtime_dir=companion_settings.runtime_dir
    )

    # A2DP address: prefer the persisted config value (set by first-time pairing)
    # over the env-var default so the Portal-saved address survives reboots.
    audio_sink = portal_cfg.audio_sink_address or companion_settings.audio.sink_address
    audio = AudioService(
        AudioSettings(sink_address=audio_sink),
        # `manager` is assigned below — safe: this closure is only called
        # from audio.run(), well after _run() finishes constructing
        # everything and hands off to the supervisor.
        speaker_state_fn=lambda: manager.snapshot.speaker_state,
    )
    pairing = PairingService(config_store, audio)
    audio_focus = AudioFocusService(
        address_fn=lambda: audio.status.address,
        pairing_active_fn=lambda: (
            pairing.status.state in (PairingState.SCANNING, PairingState.PAIRING)
        ),
        streaming_fn=audio.transport_active,
        # `manager` is assigned below — safe: this closure is only called
        # from audio_focus.run(), well after _run() finishes constructing
        # everything and hands off to the supervisor.
        ble_connected_fn=lambda: manager.snapshot.connected,
    )

    # adapter_recover_fn lets the manager clear a wedged controller
    # (scanning works, connects fail — ADR-039) by power-cycling hci0.
    # stale_reclaim_fn frees an orphaned LE control link a dead process left
    # in bluetoothd (speaker stops advertising while held → endless empty
    # scans → Portal wrongly shows "off").
    manager = DeviceManager(
        daemon_settings.speaker,
        adapter_recover_fn=reset_adapter,
        stale_reclaim_fn=disconnect_stale_speaker_links,
    )

    provisioning = ProvisioningService(companion_settings.wifi.interface)

    # Created before the routers so make_services_router can read live task
    # health via supervisor.health() — registrations happen below, after the
    # app is assembled, but Supervisor.health() only reflects what's
    # registered by the time supervisor.run() starts.
    supervisor = Supervisor()

    # Shutdown work MUST live in the ASGI lifespan, not after server.serve():
    # uvicorn (0.29+) captures SIGTERM during serve() and re-raises it the
    # moment serve() returns (Server.capture_signals → signal.raise_signal),
    # which kills the process with the default disposition before any code
    # after serve() can run. Our cleanup used to live exactly there — so on
    # every systemd restart no service was cancelled, DeviceManager never
    # disconnected, and bluetoothd kept the BLE control link alive as an
    # orphan, after which the speaker stops advertising and every scan comes
    # up empty (the wedge stale_reclaim_fn exists to break).
    #
    # supervisor_task is created after the app (it needs the assembled app's
    # services registered first); the closure reads it at shutdown time.
    supervisor_task: asyncio.Task[None] | None = None

    @asynccontextmanager
    async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if supervisor_task is not None:
                supervisor_task.cancel()
                with suppress(asyncio.CancelledError):
                    await supervisor_task

    app = create_daemon_app(
        manager,
        daemon_settings,
        audio_ready_fn=lambda: audio.audio_ready,
        audio_focus_fn=lambda: audio_focus.focus.value,
        extra_event_sources=[audio, spotify, pairing, audio_focus],
        lifespan=_lifespan,
    )
    app.include_router(make_portal_router(companion_settings, config_store))
    app.include_router(
        make_services_router(
            spotify,
            config_store,
            manager=manager,
            volume_state=volume_state,
            audio=audio,
            pairing=pairing,
            supervisor=supervisor,
            auth=make_auth_dependency(daemon_settings),
        )
    )
    app.include_router(make_wifi_router(provisioning))
    app.add_middleware(CaptivePortalMiddleware, provisioning=provisioning)

    server_config = uvicorn.Config(
        app,
        host=companion_settings.host,
        port=companion_settings.port,
        log_config=None,
    )
    server = uvicorn.Server(server_config)

    supervisor.register("device-manager", manager.run)
    supervisor.register("spotify-audio-gate", lambda: _gate_spotify_on_audio(audio, spotify))
    supervisor.register("audio-service", audio.run)
    supervisor.register(
        "ble-volume-forwarder",
        lambda: _forward_ble_volume(manager, volume_state),
        policy=RestartPolicy(initial_delay=1.0, max_delay=30.0),
    )
    supervisor.register("provisioning", provisioning.run)
    supervisor.register(
        "audio-focus",
        audio_focus.run,
        policy=RestartPolicy(initial_delay=1.0, max_delay=30.0),
    )
    supervisor.register(
        "audio-standby-recheck",
        lambda: _recheck_audio_on_standby(manager, audio),
        policy=RestartPolicy(initial_delay=1.0, max_delay=30.0),
    )
    supervisor.register(
        "idle-battery-shutdown",
        lambda: _idle_battery_shutdown(manager),
        policy=RestartPolicy(initial_delay=1.0, max_delay=30.0),
    )

    supervisor_task = asyncio.create_task(supervisor.run(), name="supervisor")

    try:
        await server.serve()
    finally:
        # Fallback for exits that never reach the lifespan (serve() raising
        # during startup); a no-op when the lifespan shutdown already ran.
        supervisor_task.cancel()
        with suppress(asyncio.CancelledError):
            await supervisor_task


if __name__ == "__main__":
    main()
