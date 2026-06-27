"""Glue that brings the A2DP audio link up to a known-good state.

Shared by all three validation scripts so the connect-and-verify sequence is
written once. Returns the live PipeWire sink node, the single thing the audio
helpers need.
"""

from __future__ import annotations

from . import audio, bluez
from .evidence import Recorder


async def resolve_audio_mac(mac: str | None, recorder: Recorder) -> str | None:
    """Use ``mac`` if given, else discover a PartyBox BR/EDR address by name."""
    if mac:
        return mac
    recorder.event("discover_start", name=bluez.PARTYBOX_NAME)
    found = await bluez.discover()
    for address, name in found:
        recorder.event("discovered", address=address, name=name)
    if not found:
        recorder.event("discover_empty")
        return None
    return found[0][0]


async def bring_up_a2dp(mac: str, recorder: Recorder) -> audio.SinkNode | None:
    """Pair (if needed), connect, and wait for the PipeWire sink node.

    Returns the sink node on success, or ``None`` with the failure recorded in
    the timeline. Idempotent: a speaker that is already bonded and connected
    short-circuits to locating its node.
    """
    current = await bluez.info(mac)
    recorder.event(
        "a2dp_info",
        known=current is not None,
        paired=getattr(current, "paired", None),
        bonded=getattr(current, "bonded", None),
        connected=getattr(current, "connected", None),
    )

    if current is None or not current.paired:
        recorder.event("a2dp_pair_attempt", mac=mac)
        pair_result = await bluez.pair(mac)
        recorder.event("a2dp_pair_result", ok=pair_result.ok, out=pair_result.stdout.strip()[:160])
        await bluez.trust(mac)

    if current is None or not current.connected:
        connect_result = await bluez.connect(mac)
        recorder.event(
            "a2dp_connect_result", ok=connect_result.ok, out=connect_result.stdout.strip()[:160]
        )
        if not await bluez.wait_connected(mac, timeout=30.0):
            recorder.event("a2dp_connect_timeout", mac=mac)
            return None

    node = await audio.wait_for_sink(mac, timeout=20.0)
    if node is None:
        recorder.event("a2dp_sink_missing", mac=mac)
        return None
    recorder.event("a2dp_sink_ready", node_id=node.node_id, codec=node.codec, profile=node.profile)
    return node
