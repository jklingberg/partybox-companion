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
The speaker keeps its **knob** position as persistent state and treats an AVRCP
absolute-volume write as **session state**. Every new A2DP transport starts from
the knob position, whatever we last wrote:

* set 52 by knob, write 70 over AVRCP, restart -> speaker reports 52 again
* floor raised 52 -> 64; next reconnect reported 52 again, and re-raised

So this floor is not a one-shot migration — it re-applies on every fresh connect,
which is what makes it self-healing and why it must stay cheap and quiet. It also
means a future AVRCP-driven volume *actuator* (the ``POST /api/v1/volume`` path)
would be giving callers session-scoped volume that silently resets to the knob
position on the next reconnect. Anything built on top has to either re-assert
after each connect or expose that reset honestly; do not assume a write sticks.

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


async def raise_amp_to_baseline(address: str, baseline: int) -> None:
    """Raise the speaker's amplifier to *baseline* if it is currently below it.

    The stage-3 half of the post-A2DP-connect volume pin (stage 2 is
    ``pipewire_volume.pin_sink_volume``). Never lowers the amplifier — see this
    module's docstring for why a floor rather than a pin. Best-effort throughout:
    every failure path logs and returns.

    Raises:
        ValueError: if *baseline* is outside [0, 127].
    """
    if not (0 <= baseline <= AVRCP_MAX):
        raise ValueError(f"baseline must be 0-{AVRCP_MAX}, got {baseline!r}")
    current = await get_amp_volume(address)
    if current is None:
        log.debug("AVRCP volume: no reported amplifier level, leaving it alone")
        return
    if current >= baseline:
        log.debug(
            "AVRCP volume: amplifier at %d/%d, at or above baseline %d — leaving it alone",
            current,
            AVRCP_MAX,
            baseline,
        )
        return
    if await set_amp_volume(address, baseline):
        log.info(
            "AVRCP volume: raised speaker amplifier %d -> %d of %d (INC-2 stage 3 floor)",
            current,
            baseline,
            AVRCP_MAX,
        )
