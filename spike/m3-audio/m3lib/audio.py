"""PipeWire audio helpers for the A2DP source path.

When a PartyBox connects as an A2DP sink, PipeWire's BlueZ backend creates an
output node named ``bluez_output.<MAC>.a2dp-sink``. These helpers locate that
node, report the negotiated codec, stream audio into it, and sample PipeWire's
xrun counters as a programmatic dropout proxy.

Playback uses ``pw-play`` targeting the node id directly, so it never depends on
a PulseAudio shim being installed (it isn't, on the Pi). A finite tone file is
generated once with ffmpeg and looped to cover arbitrary durations; loop
boundaries are recorded so a tiny gap between iterations is not misread as a
dropout.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import proc
from .evidence import Recorder


@dataclass(frozen=True)
class SinkNode:
    """A PipeWire ``bluez_output`` node for a connected speaker."""

    node_id: int
    name: str
    codec: str | None
    profile: str | None


def _mac_token(mac: str) -> str:
    # PipeWire encodes the MAC with underscores: 50:1B:.. -> 50_1B_..
    return mac.upper().replace(":", "_")


async def find_sink(mac: str) -> SinkNode | None:
    """Return the ``bluez_output`` node for ``mac`` if PipeWire has created it."""
    dump: list[dict[str, Any]] = await proc.run_json("pw-dump", timeout=15.0)
    token = _mac_token(mac)
    for obj in dump:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = obj.get("info", {}).get("props", {})
        name = props.get("node.name", "")
        if not name.startswith("bluez_output.") or token not in name.upper():
            continue
        return SinkNode(
            node_id=int(obj["id"]),
            name=name,
            codec=props.get("api.bluez5.codec"),
            profile=props.get("api.bluez5.profile"),
        )
    return None


async def wait_for_sink(mac: str, *, timeout: float = 20.0) -> SinkNode | None:
    """Poll until the speaker's sink node appears, or give up."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        node = await find_sink(mac)
        if node is not None:
            return node
        await asyncio.sleep(1.0)
    return None


async def ensure_tone(path: Path, *, seconds: int = 300, freq: int = 440) -> Path:
    """Generate a stereo sine-wave WAV at ``path`` if it does not exist."""
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    await proc.run(
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={freq}:sample_rate=44100:duration={seconds}",
        "-ac",
        "2",
        str(path),
        timeout=120.0,
        check=True,
    )
    return path


async def play(node: SinkNode, source: Path, *, recorder: Recorder) -> int:
    """Play ``source`` once into ``node``; return the ``pw-play`` exit code.

    Cancellation-safe: if the awaiting task is cancelled, the ``pw-play`` child
    is terminated rather than orphaned (an orphan keeps the speaker playing the
    whole file after the script has stopped).
    """
    child = await asyncio.create_subprocess_exec(
        "pw-play",
        "--target",
        str(node.node_id),
        str(source),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await child.communicate()
    except asyncio.CancelledError:
        child.terminate()
        with contextlib.suppress(ProcessLookupError):
            await child.wait()
        raise
    rc = child.returncode if child.returncode is not None else -1
    if rc != 0:
        recorder.event("pw_play_error", rc=rc, stderr=err.decode(errors="replace").strip()[:200])
    return rc


async def play_loop(node: SinkNode, source: Path, *, duration: float, recorder: Recorder) -> None:
    """Loop ``source`` into ``node`` until ``duration`` seconds have elapsed.

    Cancellable: cancelling the task stops playback at the next loop boundary or
    when the in-flight ``pw-play`` is interrupted.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + duration
    iteration = 0
    while loop.time() < deadline:
        iteration += 1
        recorder.event("audio_loop_start", iteration=iteration, source=source.name)
        rc = await play(node, source, recorder=recorder)
        recorder.event("audio_loop_end", iteration=iteration, rc=rc)
        if rc != 0:
            # Playback failed (likely the sink vanished); stop streaming.
            break


async def xrun_count(node_name: str) -> int | None:
    """Best-effort xrun/error count for ``node_name`` from ``pw-top -b -n 1``.

    Returns the ERR column for the node, or ``None`` if it could not be parsed.
    PipeWire's ERR counter is monotonic, so callers track the delta over time.
    """
    result = await proc.run("pw-top", "-b", "-n", "1", timeout=10.0)
    if not result.ok:
        return None
    header_cols: list[str] | None = None
    for line in result.stdout.splitlines():
        cols = line.split()
        if header_cols is None and "ERR" in cols:
            header_cols = cols
            continue
        if header_cols is not None and node_name in line:
            with contextlib.suppress(ValueError, IndexError):
                return int(cols[header_cols.index("ERR")])
    return None
