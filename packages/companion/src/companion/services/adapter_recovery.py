"""Bluetooth adapter recovery — power-cycles hci0 to clear a wedged controller.

The BCM4345 controller can enter a degraded state where LE scanning still
works but every GATT connection attempt fails (observed 2026-07-17: 22
consecutive failures over 25 minutes; see ADR-039 and
docs/validation/runs/2026-07-17-ble-wedge-investigation.md). The only known
remedies are an adapter power-cycle or a service restart — this module is the
former, so the DeviceManager can heal the condition without human help.

The D-Bus work runs in a subprocess for the same bleak/dbus-fast isolation
reasons as ``_a2dp_connect`` (all BlueZ calls from the companion process must
not share the asyncio loop with bleak's own MessageBus).
"""

from __future__ import annotations

import asyncio
import logging
import sys

log = logging.getLogger(__name__)

# The power-cycle is two D-Bus property writes and a 1 s settle; BlueZ can
# additionally take a few seconds to bring the controller back up. Well past
# this, the subprocess itself is stuck and gets killed.
_RESET_TIMEOUT = 20.0


async def reset_adapter() -> bool:
    """Power-cycle the Bluetooth adapter. Returns True if the cycle completed.

    Matches the ``adapter_recover_fn`` contract of
    :class:`partyboxd.device.manager.DeviceManager`. Never raises: every
    failure shape (spawn error, helper error line, timeout) is logged and
    collapses to False — the manager resumes its retry loop either way.

    Cancellation deliberately does NOT kill the helper subprocess: it may be
    mid-power-cycle, and killing it between Powered=false and Powered=true
    would strand the adapter off. Orphaned, it finishes the cycle on its own
    within a few seconds (and handles SIGTERM itself for the shutdown case).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "companion.services._adapter_reset",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        log.warning("adapter reset: subprocess spawn failed: %s", exc)
        return False
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_RESET_TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        log.warning("adapter reset: subprocess timed out")
        return False
    line = stdout.decode(errors="replace").strip()
    if line == "ok":
        log.info("adapter reset: hci0 power-cycled")
        return True
    log.warning(
        "adapter reset failed: %s (stderr: %s)",
        line or "<no output>",
        stderr.decode(errors="replace").strip(),
    )
    return False
