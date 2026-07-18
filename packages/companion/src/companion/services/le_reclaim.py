"""Stale-LE-connection reclaim — frees a speaker control link a dead process left behind.

If a companion process exits without disconnecting its BLE control link
(crash, SIGKILL, power loss), bluetoothd keeps the LE connection alive and
the speaker — which stops advertising while its control channel is held —
becomes undiscoverable to every subsequent scan. The DeviceManager then loops
on clean-but-empty scans indefinitely and the Portal reports the speaker as
off (observed 2026-07-17: 30+ minutes of empty scans until manual
intervention). This module is the ``stale_reclaim_fn`` injected into
:class:`partyboxd.device.manager.DeviceManager` to break that loop.

The D-Bus work runs in a subprocess for the same bleak/dbus-fast isolation
reasons as ``_a2dp_connect`` (all BlueZ calls from the companion process must
not share the asyncio loop with bleak's own MessageBus).
"""

from __future__ import annotations

import asyncio
import logging
import sys

log = logging.getLogger(__name__)

# Enumeration is one D-Bus round-trip; a disconnect of a dead-ish LE link can
# take a supervision-timeout-ish while. Well past the helper's own internal
# bounds, the subprocess itself is stuck and gets killed.
_RECLAIM_TIMEOUT = 30.0


async def disconnect_stale_speaker_links() -> bool:
    """Disconnect orphaned LE links to the speaker. Returns True if any were.

    Matches the ``stale_reclaim_fn`` contract of
    :class:`partyboxd.device.manager.DeviceManager`. Never raises: every
    failure shape (spawn error, helper error line, timeout) is logged and
    collapses to False — the manager resumes its scan loop either way.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "companion.services._le_reclaim",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        log.warning("LE reclaim: subprocess spawn failed: %s", exc)
        return False
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_RECLAIM_TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        log.warning("LE reclaim: subprocess timed out")
        return False
    line = stdout.decode(errors="replace").strip()
    if line.startswith("ok:"):
        try:
            count = int(line[len("ok:") :])
        except ValueError:
            log.warning("LE reclaim: malformed helper output %r", line)
            return False
        if count > 0:
            log.info("LE reclaim: disconnected %d stale speaker link(s)", count)
        return count > 0
    log.warning(
        "LE reclaim failed: %s (stderr: %s)",
        line or "<no output>",
        stderr.decode(errors="replace").strip(),
    )
    return False
