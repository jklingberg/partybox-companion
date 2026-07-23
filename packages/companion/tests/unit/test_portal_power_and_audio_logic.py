"""DOM-free regression tests for two Portal state bugs (#76).

The Portal (``webui/static/index.html``) has no automated test suite (see
``docs/review/05-testing-gaps.md`` TEST-01) — its state logic lives inline in
a single-file vanilla-JS app, not as importable Python. Rather than skip
regression coverage entirely, these tests extract the exact JS expressions
under test straight out of the shipped file (so a future edit that
reintroduces the bug fails here) and evaluate them with Node, without a DOM.

UX-01: ``power-toggle`` must decide on/off from ``health.speaker_state``, not
battery presence — the old ``audioAwake()`` heuristic always saw a
battery-less (mains-only) speaker as "asleep" and sent a redundant 'on'.

UX-02: the ON scene's hero caption must not read "Ready to play" while
``audio.connected`` is false (A2DP link down — WirePlumber degraded,
profile-unavailable, flap cooldown; ADR-028), even though ``deriveScene()``
itself picks Scene.ON before considering that.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_INDEX_HTML = (
    Path(__file__).resolve().parents[2] / "src" / "companion" / "webui" / "static" / "index.html"
)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _extract_braced(src: str, signature: str) -> str:
    """Return `signature` through its matching closing brace (depth-counted)."""
    start = src.index(signature)
    i = src.index("{", start)
    depth = 0
    while True:
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
        i += 1


def _extract_line(src: str, needle: str) -> str:
    for line in src.splitlines():
        if needle in line:
            return line.strip()
    raise AssertionError(f"could not find a line containing {needle!r} in index.html")


def _run_node(js: str) -> str:
    """Run `js` with node and return its stdout (expected: one JSON value via console.log)."""
    node = shutil.which("node")
    assert node is not None
    result = subprocess.run(  # noqa: S603 — fixed argv, no shell, test-only fixture data
        [node, "-e", js], capture_output=True, text=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture(scope="module")
def portal_src() -> str:
    return _INDEX_HTML.read_text()


# ---------------------------------------------------------------------------
# UX-01 — power-toggle must read health.speaker_state, not battery presence
# ---------------------------------------------------------------------------


def test_audio_awake_heuristic_is_gone(portal_src: str) -> None:
    assert "audioAwake" not in portal_src


@pytest.mark.parametrize(
    ("speaker_state", "battery", "expected_command"),
    [
        # Mains-only speaker (no battery capability at all) that IS on: the
        # old audioAwake() heuristic always read this as "asleep" and sent a
        # redundant 'on'. Must send 'off'.
        ("on", None, "off"),
        # Portable speaker, on, battery reading present: still 'off'.
        ("on", {"level": 87}, "off"),
        # Defensive: anything not reported 'on' toggles to 'on'.
        ("standby", None, "on"),
    ],
)
def test_power_toggle_reads_speaker_state_not_battery(
    portal_src: str, speaker_state: object, battery: object, expected_command: str
) -> None:
    handler_line = _extract_line(portal_src, "'power-toggle':")
    # Pull just the command expression out of `sendPower(<expr>)`.
    match = re.search(r"sendPower\((.*)\)", handler_line)
    assert match, handler_line
    command_expr = match.group(1)

    js = f"""
    const S = {{ health: {{ speaker_state: {json.dumps(speaker_state)} }},
                 battery: {json.dumps(battery)} }};
    console.log(JSON.stringify({command_expr}));
    """
    command: str = json.loads(_run_node(js))
    assert command == expected_command


# ---------------------------------------------------------------------------
# UX-02 — ON scene's hero caption must not claim "Ready to play" while
# audio.connected is false, even though deriveScene() already picked Scene.ON
# ---------------------------------------------------------------------------


def test_derive_scene_reaches_on_scene_regardless_of_audio_connected(portal_src: str) -> None:
    """Documents the precondition UX-02's caption fix guards against:
    deriveScene() alone does not distinguish a live A2DP link from a dead one.
    """
    scene_decl = re.search(r"const Scene = Object\.freeze\(\{.*?\}\);", portal_src, re.S)
    assert scene_decl
    derive_scene_fn = _extract_braced(portal_src, "function deriveScene() {")

    js = f"""
    {scene_decl.group(0)}
    {derive_scene_fn}
    const base = {{ health: {{ speaker_state: 'on', audio_focus: 'exclusive' }},
                     powerPending: null, btResetStartedAt: null,
                     audio: {{ address: '50:1B:6A:14:FD:1D' }} }};
    const connected = {{ ...base, audio: {{ ...base.audio, connected: true }} }};
    const degraded  = {{ ...base, audio: {{ ...base.audio, connected: false }} }};
    global.S = connected;
    const sceneConnected = deriveScene();
    global.S = degraded;
    const sceneDegraded = deriveScene();
    console.log(JSON.stringify([sceneConnected, sceneDegraded]));
    """
    scenes: list[str] = json.loads(_run_node(js))
    scene_connected, scene_degraded = scenes
    assert scene_connected == "on"
    assert scene_degraded == "on"


def test_hero_caption_is_authoritative_over_ready_to_play(portal_src: str) -> None:
    audio_up_line = _extract_line(portal_src, "const audioUp =")
    caption_line = _extract_line(portal_src, "caption.textContent =")

    def caption_for(audio: object, spotify: object) -> str:
        js = f"""
        const S = {{ audio: {json.dumps(audio)}, spotify: {json.dumps(spotify)} }};
        {audio_up_line}
        const playing = audioUp && S.spotify && S.spotify.state === 'playing';
        const paused = audioUp && S.spotify && S.spotify.state === 'paused';
        let captionText;
        {caption_line.replace("caption.textContent", "captionText")}
        console.log(JSON.stringify(captionText));
        """
        caption: str = json.loads(_run_node(js))
        return caption

    # A2DP link down: must never read "Ready to play", regardless of what
    # Spotify last reported (it can't actually be reaching the speaker).
    assert caption_for({"connected": False}, {"state": "playing"}) != "Ready to play"
    assert caption_for({"connected": False}, None) == "Connecting audio…"
    # A2DP link up, idle: the real "Ready to play" case.
    assert caption_for({"connected": True}, None) == "Ready to play"
