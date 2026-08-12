"""AVRCP absolute-volume actuator — the speaker's own amplifier.

This is the third and loudest stage of the appliance's volume chain, and until
now the only one no code touched:

===  ====================================  ==========================
1    Spotify slider -> librespot softvol    ``services.spotify`` flags
2    PipeWire A2DP sink node                ``services.pipewire_volume``
3    **speaker amplifier (AVRCP)**          **this module**
===  ====================================  ==========================

INC-2 (``docs/validation/runs/2026-07-02-rc13.md``) was filed as "music is far
quieter than the speaker's own native sounds". Stages 1 and 2 were fixed first,
but the symptom survived a fresh SD flash because stage 3 was still whatever the
speaker happened to remember — measured at **40/127 (31%)** on hardware
(2026-08-12), with the operator confirming audibly that raising it was the
missing loudness. The appliance controlled two thirds of the chain and inherited
the loudest third.

Stage 3 is also the only *loss-free* stage. Stages 1 and 2 are digital gain
applied before an S16 quantise, so every 6 dB of attenuation costs about a bit
of resolution, and neither can exceed unity without clipping. The AVRCP level
acts inside the speaker's own amplifier/DSP, after every digital stage — so
loudness headroom should always be taken here first, and only then from the
digital stages.

Why this raises but never lowers
--------------------------------
:func:`raise_amp_to_baseline` is a **floor**, not a pin. The speaker's volume
knob is a legitimate volume authority under ADR-022's last-write-wins model, and
the A2DP link drops and re-establishes routinely (see ``AudioService``'s module
docstring). A pin that slammed the amplifier to a fixed level on every reconnect
would fight the operator's knob every few minutes, and — since this stage is the
loud one — would do it audibly and unpleasantly. Raising only when the speaker
reports *below* the baseline fixes the fresh-install floor that INC-2 is actually
about while leaving any deliberate adjustment above it alone.

What the speaker persists (hardware, 2026-08-12)
------------------------------------------------
Characterised on hardware by controlled experiment (2026-08-12). The speaker's
**knob** writes a persistent store; an AVRCP write is scoped to the current A2DP
connection:

=========================  ============================================
Knob move                  writes the speaker's persistent store
AVRCP write                valid for the current A2DP connection
Stream ``idle``/``active`` **survives** — 52 held across a 2-min teardown
Time passing               **survives** — 88 held for 8 min, 28 samples
A2DP reconnect             **lost** — reverts to the knob value
Speaker power cycle        **lost** — reverts to the knob value
=========================  ============================================

The last two are why this module exists: on a power cycle with the knob at 20,
the speaker came back reporting 20 and the floor restored the baseline unprompted.

The first two are why nothing more than a connect-time write is needed, and are
worth stating as explicit negative results because the obvious next instinct is
to add maintenance. There is no mid-session drift to correct: a periodic
re-assert (tried, reverted — see the PR discussion) and a transport-state hook
were both tested against hardware and neither had a fault to fix. The value is
lost exactly and only when the connection is remade, which is exactly when
``pin_volume_fn`` already fires. Do not add polling here without new evidence.

The same model governs anything built on top: an AVRCP-driven volume *actuator*
(the ``POST /api/v1/volume`` path) would give callers volume that holds for the
session but silently returns to the knob position on the next reconnect. It has
to re-assert on connect, or expose that reset honestly.

The read is a cache, not a measurement
--------------------------------------
``MediaTransport1.Volume`` is BlueZ's cached view, updated when the speaker sends
an AVRCP notification — it is not a query. Two consequences, both of which have
bitten this module:

* It can be **absent** on a fresh connect, until the peer's first notification
  (see ``_VOLUME_WAIT_ATTEMPTS``).
* It can be **stale**: after a reconnect it may still hold the value we wrote
  last session while the speaker has reverted to its knob level. Observed with
  the knob at 20 and BlueZ reporting 52.

So never treat a read as proof of the speaker's state. In particular, do not skip
a write because the read already matches what you were about to write — that is
indistinguishable from a stale cache, and is why
:func:`raise_amp_to_baseline` compares strictly greater rather than
greater-or-equal.

Units
-----
AVRCP absolute volume is a 0-127 scale (``org.bluez.MediaTransport1.Volume``).
Its mapping to dB is device-specific and undocumented, so the baseline cannot be
derived from the digital gains in stages 1-2 — the two live in different,
non-convertible units. It is an empirical number, set by listening.

All calls are best-effort. No A2DP transport, no ``Volume`` property (a speaker
implementing no absolute volume at all), or a BlueZ error are all normal,
expected conditions rather than failures — the appliance stays audibly usable at
whatever level the speaker already had.
"""

from __future__ import annotations

import asyncio
import logging
import sys

log = logging.getLogger(__name__)

_TIMEOUT = 10.0

#: Highest value the AVRCP absolute-volume scale can express.
AVRCP_MAX = 127

# BlueZ only creates MediaTransport1.Volume once the peer has sent its first
# AVRCP volume notification, and AVRCP/AVCTP setup can lag the AVDTP transport
# that AudioService waits for (_POST_CONNECT_SETTLE, 5s). Reading exactly once
# therefore races that window: lose it, and the floor silently skips for the
# whole session while the amplifier sits wherever the speaker left it — the
# original INC-2 symptom, intermittently, and invisible above DEBUG.
#
# So the read is retried briefly. Bounded rather than open-ended because a
# speaker that implements no absolute volume at all never grows the property,
# and that case is indistinguishable from "not yet" (both read as "none"). Kept
# short deliberately: this runs inside AudioService's connect loop, which cannot
# service recheck/retry nudges while it waits.
_VOLUME_WAIT_ATTEMPTS = 4
_VOLUME_WAIT_DELAY = 1.5


async def _run_helper(address: str, command: str) -> str | None:
    """Run the BlueZ helper in a subprocess and return its single output line.

    BlueZ calls go through ``companion.services._a2dp_connect`` in a subprocess
    for the same reason every other BlueZ call in this package does: bleak holds
    its own dbus-fast ``MessageBus`` in the companion event loop, and running
    BlueZ calls on that loop lets the two buses interact. Returns ``None`` if the
    helper could not be run at all.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "companion.services._a2dp_connect",
            address,
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        log.debug("AVRCP volume: helper %s could not start: %s", command, exc)
        return None
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        log.warning("AVRCP volume: helper %s timed out", command)
        return None
    if stderr:
        log.debug(
            "AVRCP volume: helper %s stderr: %s", command, stderr.decode(errors="replace").strip()
        )
    return stdout.decode(errors="replace").strip()


async def get_amp_volume(address: str) -> int | None:
    """Return the speaker's AVRCP absolute volume (0-127), or ``None``.

    ``None`` means "nothing to report": no A2DP transport for *address*, the
    peer exposes no ``Volume`` property, or BlueZ errored. Callers must not treat
    it as zero.
    """
    line = await _run_helper(address, "volume")
    if line is None or line == "none":
        return None
    try:
        level = int(line)
    except ValueError:
        log.debug("AVRCP volume: unparseable helper output %r", line)
        return None
    if not (0 <= level <= AVRCP_MAX):
        log.warning("AVRCP volume: helper reported out-of-range level %d", level)
        return None
    return level


async def set_amp_volume(address: str, level: int) -> bool:
    """Set the speaker's AVRCP absolute volume to *level* (0-127).

    Returns whether BlueZ *accepted* the write — which is not the same as the
    speaker having applied it. BlueZ caches the value and forwards it over AVRCP;
    the speaker applies it asynchronously (~2-10s observed on hardware) and then
    re-notifies its own level, which overwrites the cache. So a read-back
    immediately after this returns reflects our own write, not the speaker's
    state, and proves nothing. Verify by listening, or by reading well after the
    speaker has had time to notify.

    Raises:
        ValueError: if *level* is outside [0, 127].
    """
    if not (0 <= level <= AVRCP_MAX):
        raise ValueError(f"level must be 0-{AVRCP_MAX}, got {level!r}")
    line = await _run_helper(address, f"volume={level}")
    if line == "ok":
        return True
    log.warning("AVRCP volume: set to %d failed: %s", level, line or "no helper output")
    return False


async def _wait_for_amp_volume(address: str) -> int | None:
    """Read the amplifier level, retrying while the peer has not reported one yet.

    Returns the level, or ``None`` once the attempts are exhausted. Does not sleep
    after the final attempt, and returns immediately on the first success — so the
    common case (property already present) costs exactly one read.
    """
    for attempt in range(1, _VOLUME_WAIT_ATTEMPTS + 1):
        level = await get_amp_volume(address)
        if level is not None:
            if attempt > 1:
                log.debug("AVRCP volume: level appeared on attempt %d", attempt)
            return level
        if attempt < _VOLUME_WAIT_ATTEMPTS:
            await asyncio.sleep(_VOLUME_WAIT_DELAY)
    return None


async def raise_amp_to_baseline(address: str, baseline: int) -> None:
    """Raise the speaker's amplifier to *baseline* if it is currently below it.

    The stage-3 half of the post-A2DP-connect volume pin (stage 2 is
    ``pipewire_volume.pin_sink_volume``). Never lowers the amplifier — see this
    module's docstring for why a floor rather than a pin. Best-effort throughout:
    every failure path logs and returns.

    Retries the read for up to ``_VOLUME_WAIT_ATTEMPTS`` attempts, because the
    ``Volume`` property can appear after the transport does — see the constants.

    Raises:
        ValueError: if *baseline* is outside [0, 127].
    """
    if not (0 <= baseline <= AVRCP_MAX):
        raise ValueError(f"baseline must be 0-{AVRCP_MAX}, got {baseline!r}")
    current = await _wait_for_amp_volume(address)
    if current is None:
        # Not DEBUG: this means the floor did not apply, so music may be quiet for
        # the rest of the session. Worth one visible line, since the alternative is
        # a silent recurrence of INC-2 with nothing in the log to point at.
        log.info(
            "AVRCP volume: no absolute volume reported after %d attempts over %.1fs — "
            "amplifier left as-is (the speaker may not implement absolute volume)",
            _VOLUME_WAIT_ATTEMPTS,
            _VOLUME_WAIT_DELAY * (_VOLUME_WAIT_ATTEMPTS - 1),
        )
        return
    # Strictly greater, deliberately: a reading that *equals* the baseline is not
    # trustworthy, because the baseline is the only value this function ever
    # writes. BlueZ's Volume is a cache, and on a fresh connect it can still hold
    # the previous session's written value while the speaker has reverted to its
    # knob level — so "already at the baseline" is exactly what a stale cache
    # looks like. Observed on hardware 2026-08-12: knob at 20, BlueZ reporting 52,
    # the floor skipping, and the amplifier audibly at 20 for the whole session.
    #
    # Writing on equality costs one redundant AVRCP write per connect and closes
    # that hole. A reading strictly above the baseline is still respected: that
    # cannot have come from this function, so it is a genuine knob-up, and the
    # knob is the operator's way up (see AudioSettings.amp_baseline).
    if current > baseline:
        log.debug(
            "AVRCP volume: amplifier at %d/%d, above baseline %d — leaving it alone",
            current,
            AVRCP_MAX,
            baseline,
        )
        return
    if await set_amp_volume(address, baseline):
        # Deliberately "requested", not "raised": BlueZ accepting the write is not
        # the speaker having applied it (see set_amp_volume), and this line is read
        # while debugging volume complaints — it must not assert a level we never
        # confirmed.
        log.info(
            "AVRCP volume: requested speaker amplifier %d -> %d of %d "
            "(INC-2 stage 3 floor; BlueZ accepted, applied asynchronously)",
            current,
            baseline,
            AVRCP_MAX,
        )
