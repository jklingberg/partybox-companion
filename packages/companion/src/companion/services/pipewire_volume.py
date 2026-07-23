"""PipeWire volume actuator — the real mechanism behind the volume API.

BLE hardware volume (``partybox.device.capabilities.volume.VolumeCapability``)
raises ``NotImplementedError`` until the opcode is confirmed (see ADR-022
Phase 2). Until then, this module is what actually changes and reads audible
output volume: the PipeWire node created for the A2DP Bluetooth sink, via
``wpctl`` (PipeWire's own CLI, part of the ``pipewire-utils`` no additional
IPC/session bus needed, unlike BlueZ D-Bus calls elsewhere in this package).

The appliance has exactly one audio output, so every call here targets
``@DEFAULT_AUDIO_SINK@`` — PipeWire's well-known name for whichever sink is
currently default — rather than resolving the bluez node by MAC address.
This is the same target ADR-028's "Volume floor from mixin.stateless"
investigation validated by hand (``wpctl get-volume @DEFAULT_AUDIO_SINK@``)
after an A2DP connect. A future multi-output appliance would need a real
resolver (e.g. by node name/MAC) in place of this constant — not a concern
today, since there is exactly one sink to ever be "default".

Both functions are best-effort: PipeWire being unreachable (no ``wpctl``
binary, no default sink yet, a timeout) is a normal, expected condition
before A2DP connects, not an error — callers treat ``None``/``False`` as
"nothing to report" and fall back accordingly (see
``companion.services.router``).
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

_DEFAULT_SINK = "@DEFAULT_AUDIO_SINK@"
_TIMEOUT = 5.0


async def set_volume(percent: int) -> bool:
    """Set the default PipeWire sink volume to *percent* (0-100).

    Returns ``True`` if ``wpctl`` reported success, ``False`` otherwise
    (PipeWire not running, no default sink, ``wpctl`` missing, timeout).

    Raises:
        ValueError: if *percent* is outside [0, 100].
    """
    if not (0 <= percent <= 100):
        raise ValueError(f"percent must be 0-100, got {percent!r}")
    fraction = f"{percent / 100:.2f}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "wpctl",
            "set-volume",
            _DEFAULT_SINK,
            fraction,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT)
    except (OSError, TimeoutError) as exc:
        log.warning("PipeWire volume: set-volume %s%% failed: %s", percent, exc)
        return False
    if proc.returncode != 0:
        log.warning(
            "PipeWire volume: wpctl set-volume exited %d: %s",
            proc.returncode,
            stderr.decode(errors="replace").strip(),
        )
        return False
    return True


async def get_volume() -> int | None:
    """Return the default PipeWire sink volume as a percentage (0-100).

    Returns ``None`` if PipeWire has no default sink, ``wpctl`` is
    unavailable, or its output could not be parsed.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "wpctl",
            "get-volume",
            _DEFAULT_SINK,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT)
    except (OSError, TimeoutError) as exc:
        log.debug("PipeWire volume: get-volume failed: %s", exc)
        return None
    if proc.returncode != 0:
        return None
    # Expected form: "Volume: 0.53" (optionally "Volume: 0.53 [MUTED]").
    line = stdout.decode(errors="replace").strip()
    prefix = "Volume:"
    if not line.startswith(prefix):
        return None
    try:
        level = float(line[len(prefix) :].split()[0])
    except ValueError, IndexError:
        return None
    # WirePlumber allows boosted volume above 1.0 (e.g. a manual
    # `wpctl set-volume ... 1.2`, outside this module's control) — clamp so
    # callers relying on the documented 0-100 contract (VolumeResponse, the
    # Portal slider) never see an out-of-range value.
    return max(0, min(100, round(level * 100)))


async def pin_sink_volume(known_level: int | None) -> None:
    """Pin the fresh PipeWire sink to *known_level* after an A2DP connect.

    Fixes INC-2 (docs/validation/runs/2026-07-02-rc13.md): WirePlumber
    defaults every newly-created A2DP sink node to ~40% volume
    (docs/adr/028-audio-readiness-model.md § "Volume floor from
    mixin.stateless"), so music played quieter than the speaker's own native
    sounds on every fresh install. Explicit here (rather than relying solely
    on the image-level WirePlumber config override) per ARCH-04's
    recommendation — this is the actuator ``AudioService`` calls via
    ``pin_volume_fn``, injected from ``companion.__main__``; failures are
    logged and swallowed by the caller (``AudioService._pin_volume``).

    Targets *known_level* rather than a hardcoded 100 when the caller
    already has one (the last value recorded in ``VolumeState`` — this
    function stays agnostic of that type so the actuator module doesn't need
    to know about the domain state model): A2DP reconnects routinely (the
    speaker drops the BR/EDR link on idle — see ``AudioService``'s module
    docstring), and slamming a reconnect to 100% would clobber whatever
    level the user or Spotify last set, violating ADR-022's last-write-wins
    model. 100% remains the target only when nothing has been recorded yet
    (a true fresh boot/pairing, the case INC-2 was actually about).
    """
    await set_volume(known_level if known_level is not None else 100)
