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
        parts = line[len("ok:") :].split(":")
        try:
            count = int(parts[0])
        except ValueError:
            log.warning("LE reclaim: malformed helper output %r", line)
            return False
        # Parsed separately from count: a malformed/missing seen field (old-
        # format "ok:<n>" output, or version skew between this wrapper and
        # the _le_reclaim helper) must not mask an otherwise-valid count —
        # that would report a real successful reclaim as a failure and leave
        # the retry backoff in place instead of resetting it.
        seen: int | None = None
        if len(parts) > 1:
            try:
                seen = int(parts[1])
            except ValueError:
                log.warning("LE reclaim: malformed seen count in helper output %r", line)
        if count > 0:
            log.info("LE reclaim: disconnected %d stale speaker link(s)", count)
        elif seen == 0:
            log.debug("LE reclaim: no PartyBox-named LE device objects in BlueZ's cache")
        elif seen is not None:
            # Previously silent — during a 2026-07-23 outage this path ran an
            # estimated 40+ times with no way to tell "BlueZ has no record of
            # the speaker" apart from "saw it, but not connected".
            log.debug(
                "LE reclaim: checked %d PartyBox-named LE device object(s) on "
                "our adapter, none connected — nothing to reclaim",
                seen,
            )
        # else: seen is unknown (old-format output, or its field didn't
        # parse) — no diagnostic claim to make either way.
        return count > 0
    log.warning(
        "LE reclaim failed: %s (stderr: %s)",
        line or "<no output>",
        stderr.decode(errors="replace").strip(),
    )
    return False
