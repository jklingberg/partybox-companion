"""DOM-free regression tests for three Portal state bugs (#76).

The Portal (``webui/static/index.html``) is a deliberately single-file
vanilla-JS app (see ``docs/design/portal-redesign.md``) with no build step and
no test suite yet (``docs/review/05-testing-gaps.md`` TEST-01). There is
nowhere else for this logic to live as importable, unit-testable Python or a
separate JS module without breaking that single-file deployment model — the
whole point is that ``rsync``-ing one HTML file to the appliance and
restarting the service is enough to deploy a Portal change (see CLAUDE.md's
"Deploying source changes to the Pi").

So instead these tests extract the *exact* JS under test straight out of the
shipped file and evaluate it with Node, without a DOM. This trades some
sensitivity to unrelated refactors (renaming an extracted function, or moving
a fragment out of the function it currently lives in) for the alternative of
no regression coverage at all for logic that has already shipped two real
bugs. Two things keep that fragility in check:

- Prefer extracting whole named functions (``_extract_braced``) over fragile
  single-line string matches (``_extract_line``) wherever the target logic is
  already a standalone function — a whole-function extraction survives
  internal reformatting/reordering as long as the function's name and
  observable behavior don't change, which is exactly what these tests care
  about. ``power_toggle_command()``, ``deriveScene()`` and ``healthItems()``
  (plus its ``companionHealthItem()``/``humanizeTaskName()`` dependencies) are
  all pure functions of ``S`` for this reason.
- Line-based extraction is a last resort, used only for the two hero-caption
  expressions in ``patchOnScene()`` that can't be extracted as a standalone
  function (the function is full of ``document.getElementById`` DOM calls
  this test harness has no DOM for). Its search is scoped to
  ``patchOnScene()``'s own body (not the whole file) specifically because
  ``audioUp`` is computed the same way in more than one function now — a
  file-wide search would silently match whichever occurrence happens to come
  first in the file, which is exactly the kind of hidden-order-dependency
  fragility to avoid.

If a change to index.html breaks one of these tests, that is a signal to
look at *why* — a real regression should fail loudly with a clear diff; a
harmless rename/reorder should be rare given the above, and if it happens,
updating the extraction target here is a one-line fix.

UX-01: the power toggle must decide on/off from ``health.speaker_state``, not
battery presence — the old ``audioAwake()`` heuristic always saw a
battery-less (mains-only) speaker as "asleep" and sent a redundant 'on'.

UX-02: nothing in the ON scene may claim audio is working while
``audio.connected`` is false (A2DP link down — WirePlumber degraded,
profile-unavailable, flap cooldown; ADR-028), even though ``deriveScene()``
itself picks Scene.ON before considering that: covers the hero caption, the
Spotify Connect source card, and the Spotify Connect row in the health sheet
— all three used to (or, for the caption, could have) claimed things were
fine.
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
    matches = [line.strip() for line in src.splitlines() if needle in line]
    assert len(matches) == 1, (
        f"expected exactly one line containing {needle!r}, found {len(matches)} "
        "(pass a narrower `src` — e.g. one function's body — to disambiguate)"
    )
    return matches[0]


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
# UX-01 — power toggle must read health.speaker_state, not battery presence
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
def test_power_toggle_command_reads_speaker_state_not_battery(
    portal_src: str, speaker_state: object, battery: object, expected_command: str
) -> None:
    fn = _extract_braced(portal_src, "function powerToggleCommand() {")

    js = f"""
    const S = {{ health: {{ speaker_state: {json.dumps(speaker_state)} }},
                 battery: {json.dumps(battery)} }};
    {fn}
    console.log(JSON.stringify(powerToggleCommand()));
    """
    command: str = json.loads(_run_node(js))
    assert command == expected_command


# ---------------------------------------------------------------------------
# UX-02 — nothing in the ON scene may claim audio works while audio.connected
# is false, even though deriveScene() already picked Scene.ON
# ---------------------------------------------------------------------------


def test_derive_scene_reaches_on_scene_regardless_of_audio_connected(portal_src: str) -> None:
    """Documents the precondition every UX-02 fix guards against:
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
    patch_on_scene_fn = _extract_braced(portal_src, "function patchOnScene() {")
    audio_up_line = _extract_line(patch_on_scene_fn, "const audioUp =")
    caption_line = _extract_line(patch_on_scene_fn, "caption.textContent =")

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


def test_health_sheet_spotify_row_reflects_audio_connected(portal_src: str) -> None:
    """The health sheet's "Spotify Connect" row used to read state 'ok' /
    "Playing" purely from S.spotify.state, ignoring S.audio.connected — so it
    could sit right next to a correctly-degraded "Bluetooth Audio: Connecting…"
    row while itself still claiming a plain green "ok".
    """
    task_name_acronyms = _extract_line(portal_src, "const TASK_NAME_ACRONYMS =")
    humanize_fn = _extract_braced(portal_src, "function humanizeTaskName(name) {")
    companion_item_fn = _extract_braced(portal_src, "function companionHealthItem() {")
    health_items_fn = _extract_braced(portal_src, "function healthItems() {")

    def spotify_row(*, audio_connected: bool, spotify_state: str) -> dict[str, str]:
        js = f"""
        const S = {{
          audio: {{ address: '50:1B:6A:14:FD:1D', connected: {json.dumps(audio_connected)} }},
          spotify: {{ running: true, state: {json.dumps(spotify_state)} }},
          health: {{ audio_focus: 'exclusive' }},
          healthDetails: {{ tasks: [] }},
        }};
        {task_name_acronyms}
        {humanize_fn}
        {companion_item_fn}
        {health_items_fn}
        const row = healthItems().find(i => i.name === 'Spotify Connect');
        console.log(JSON.stringify({{ state: row.state, text: row.text }}));
        """
        result: dict[str, str] = json.loads(_run_node(js))
        return result

    # A2DP link down, librespot still reporting 'playing': must not read 'ok'
    # / "Playing" — that claim can't be backed up with the link down.
    degraded = spotify_row(audio_connected=False, spotify_state="playing")
    assert degraded["state"] != "ok"
    assert degraded["text"] != "Playing"

    # A2DP link up: the real "Playing" case, unaffected.
    up = spotify_row(audio_connected=True, spotify_state="playing")
    assert up["state"] == "ok"
    assert up["text"] == "Playing"
