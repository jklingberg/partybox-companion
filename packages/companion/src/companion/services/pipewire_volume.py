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
after an A2DP connect.

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
