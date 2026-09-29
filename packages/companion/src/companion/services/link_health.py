"""Observed Bluetooth link faults, counted from the journal.

The companion's existing signals cannot tell a healthy A2DP link from one
that is audibly breaking up. During the 2026-09-01 incident the appliance
reported ``ble_connected: true``, ``audio_ready: true``, PipeWire reported
**zero** xruns, and the SoC was neither hot nor throttled — while the music
stuttered continuously. Frames were handed to the kernel on time; they were
lost on the air afterwards.

So xruns are the wrong metric, and this module deliberately does not use
them. What did track the fault, from the same capture:

===========================  ==============  =============
Window                       L2CAP errors    Rate
===========================  ==============  =============
14:05:33-14:09:30 (faulty)   6               ~90/hour
14:09:30-17:07   (recovered) 3               ~1/hour
===========================  ==============  =============

and the correlation with A2DP failures was near-instant — a
``br-connection-unknown`` at 14:09:07 and a kernel L2CAP error in the same
second. Both counters are read here from the journal the service can already
see (``SupplementaryGroups=systemd-journal``); no new privileges, and nothing
is put on the air to measure it. See ADR-044.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger(__name__)

Level = Literal["ok", "warn", "err"]

#: How far back the counters look. Long enough that a single bad patch does
#: not dominate, short enough that a fault the user is hearing *now* is still
#: in the window.
_WINDOW = "-1h"
_WINDOW_HOURS = 1.0

#: Kernel signature of L2CAP reassembly failing on a damaged packet. This is
#: the direct evidence of on-air corruption — the thing that is audible as
#: stutter and invisible to every userspace signal.
_L2CAP_MATCH = "Unexpected continuation frame"

#: Companion-side link faults: the control link dropping, and A2DP refusing
#: to (re)establish. Counted together as "the link did not hold".
_A2DP_MATCH = "connection lost, will reconnect|A2DP connect failed"

#: Errors per hour at which the link is warned about / called bad.
#: Calibrated against the one capture quantified above (~1/hour recovered,
#: ~90/hour during the fault), so the gap between the thresholds is wide on
#: purpose — the honest claim these support is "quiet" versus "clearly
#: faulty", not a fine-grained quality score. Expect to revisit once this has
#: seen more than one RF environment.
_L2CAP_WARN = 5
_L2CAP_ERR = 30

#: Link drops per hour. A couple of reconnects an hour is ordinary on BLE
#: with a speaker that rotates its address; a steady stream is not.
_A2DP_WARN = 2
_A2DP_ERR = 6

#: Results are reused for this long. The Portal reconciles every 30 s and
#: each sample costs two journalctl subprocesses, so without this the page
#: being open would spawn four processes a minute for data that cannot
#: meaningfully change that fast.
_CACHE_TTL = 25.0


@dataclass(frozen=True)
class LinkHealth:
    """Bluetooth link faults observed over the last :data:`_WINDOW_HOURS`.

    ``available`` is False when the journal could not be read at all, which
    is distinct from a clean window — a caller must not render "no faults"
    from a reading that never happened.
    """

    available: bool
    l2cap_errors: int
    a2dp_drops: int
    window_hours: float
    level: Level


_UNAVAILABLE = LinkHealth(
    available=False, l2cap_errors=0, a2dp_drops=0, window_hours=_WINDOW_HOURS, level="ok"
)

_cache: tuple[float, LinkHealth] | None = None


async def sample(*, now: float | None = None) -> LinkHealth:
    """Return recent link-fault counts, reusing a recent sample if fresh."""
    global _cache
    stamp = time.monotonic() if now is None else now
    if _cache is not None and stamp - _cache[0] < _CACHE_TTL:
        return _cache[1]

    l2cap = await _count("--dmesg", _L2CAP_MATCH)
    a2dp = await _count("--unit=companion", _A2DP_MATCH)
    if l2cap is None and a2dp is None:
        result = _UNAVAILABLE
    else:
        result = _classify(l2cap or 0, a2dp or 0)
    _cache = (stamp, result)
    return result


def reset_cache() -> None:
    """Drop the memoised sample. For tests and for forcing a fresh read."""
    global _cache
    _cache = None


def _classify(l2cap: int, a2dp: int) -> LinkHealth:
    """Grade the counts, worst-of the two signals.

    Worst-of rather than a weighted blend: the two failures are alternative
    symptoms of one problem, not independent contributions to it. A link
    corrupting packets without dropping, and one dropping without visible
    corruption, are both bad, and neither should be averaged down by the
    other looking fine.
    """
    level: Level = "ok"
    if l2cap >= _L2CAP_ERR or a2dp >= _A2DP_ERR:
        level = "err"
    elif l2cap >= _L2CAP_WARN or a2dp >= _A2DP_WARN:
        level = "warn"
    return LinkHealth(
        available=True,
        l2cap_errors=l2cap,
        a2dp_drops=a2dp,
        window_hours=_WINDOW_HOURS,
        level=level,
    )


async def _count(source: str, pattern: str) -> int | None:
    """Count journal lines matching *pattern* in *source* over the window.

    Returns ``None`` only when the journal could not be read at all. A
    non-zero exit is *not* treated as failure: journalctl exits 1 when a
    ``--grep`` matches nothing, which is the commonest healthy case and must
    read as zero rather than as "unavailable".
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "journalctl",
            source,
            f"--since={_WINDOW}",
            "--grep",
            pattern,
            "--case-sensitive=no",
            "--no-pager",
            "--output=cat",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except OSError, TimeoutError:
        log.debug("link health: journalctl unavailable for %s", source)
        return None
    text = (stdout or b"").decode(errors="replace")
    return sum(1 for line in text.splitlines() if line.strip())
