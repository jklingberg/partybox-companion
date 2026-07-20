"""Deterministic, headed Playwright walkthrough of the Companion Portal.

This does not test the app — it drives a real browser through a fixed,
human-paced sequence of interactions against the Portal's built-in `?mock`
demo mode, so the result can be screen-recorded into a README GIF. No
PartyBox hardware or running daemon is required.

Run with `companion-demo` (see demo/README.md) or directly:

    uv run companion-demo
    uv run companion-demo --headless   # smoke-test the sequence, no window
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import http.server
import json
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

from playwright.sync_api import Locator, Page, Route, sync_playwright

# ---------------------------------------------------------------------------
# Canned "healthy appliance" responses.
#
# `?mock` already seeds the Portal's own in-page state with this exact data
# (see MOCK_* in webui/static/index.html) — the app renders a fully healthy
# scene on load with no network calls at all. These mirror that data and
# exist only as a safety net: a couple of code paths (saveSettings's
# post-save refresh()) issue real fetch() calls even in mock mode, and
# without a backend those would 404 and silently blank out fields like
# battery. Intercepting every /api/v1/** call keeps the demo deterministic
# regardless of which internal paths fire.
# ---------------------------------------------------------------------------
_HEALTH = {
    "status": "ok",
    "version": "0.2.0-dev",
    "ble_connected": True,
    "audio_ready": True,
    "speaker_state": "on",
    "audio_focus": "exclusive",
}
_HEALTH_DETAILS = {
    "tasks": [
        {"name": "device-manager", "state": "running", "last_exception": None, "total_failures": 0},
        {"name": "audio-service", "state": "running", "last_exception": None, "total_failures": 0},
        {
            "name": "spotify-audio-gate",
            "state": "running",
            "last_exception": None,
            "total_failures": 0,
        },
    ]
}
_SPEAKER = {"connected": True, "address": "AA:BB:CC:DD:EE:FF", "firmware": "26.2.10", "battery": 87}
_BATTERY = {
    "level": 87,
    "power_source": "battery",
    "charging": False,
    "remaining_playtime_minutes": 612,
    "state_of_health_percent": 99,
    "cycle_count": 4,
}
_AUDIO = {"connected": True, "address": "50:1B:6A:14:FD:1D", "pairing_state": "idle"}

# Mutated in place by the settings-save step so the demo's "changed name"
# actually sticks for the rest of the run.
_config_state = {"spotify_connect_name": "Living Room", "spotify_bitrate": 320}
_spotify_state = {"running": True, "active": True, "device_name": "Living Room"}

_NEW_DEVICE_NAME = "Backyard"

# A small dot that tracks real cursor position via `mousemove`. Playwright's
# synthetic mouse events don't reliably paint an OS cursor sprite across
# platforms, so we draw our own — purely cosmetic, doesn't touch app state.
#
# Also hides the "MOCK MODE" banner: it's correct and useful during UI
# development, but it would give the game away in a README GIF meant to
# look like a real, healthy appliance.
_CURSOR_SCRIPT = """
(() => {
  // add_init_script fires as early as "document created" — sometimes before
  // document.documentElement/head/body exist yet — so every DOM touch here
  // is guarded, and setup runs both immediately and again on
  // DOMContentLoaded to cover whichever moment actually has a usable DOM.
  function injectStyle() {
    const root = document.head || document.documentElement;
    if (!root || root.querySelector('#__demo_style')) return;
    const style = document.createElement('style');
    style.id = '__demo_style';
    style.textContent = '#mock-banner { display: none !important; }';
    root.appendChild(style);
  }
  function attachCursor() {
    if (!document.body || document.getElementById('__demo_cursor')) return;
    const dot = document.createElement('div');
    dot.id = '__demo_cursor';
    Object.assign(dot.style, {
      position: 'fixed', width: '16px', height: '16px', borderRadius: '50%',
      background: 'rgba(255,255,255,0.92)', border: '2px solid rgba(0,0,0,0.55)',
      pointerEvents: 'none', zIndex: 2147483647, transform: 'translate(-50%, -50%)',
      left: '-100px', top: '-100px', boxShadow: '0 1px 4px rgba(0,0,0,0.35)',
    });
    document.body.appendChild(dot);
  }
  const ready = () => { injectStyle(); attachCursor(); };
  document.addEventListener('DOMContentLoaded', ready);
  ready();
  window.addEventListener('mousemove', (e) => {
    const dot = document.getElementById('__demo_cursor');
    if (dot) { dot.style.left = e.clientX + 'px'; dot.style.top = e.clientY + 'px'; }
  });
})();
"""


def _find_static_dir() -> Path:
    """Locate webui/static by walking up from this file — works regardless
    of whether it's run from a source checkout or an installed wheel."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "packages" / "companion" / "src" / "companion" / "webui" / "static"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        "Could not find packages/companion/src/companion/webui/static — "
        "run this from within a partybox-companion checkout."
    )


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


@contextlib.contextmanager
def _static_server(directory: Path) -> Iterator[str]:
    handler = functools.partial(_QuietHandler, directory=str(directory))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _handle_api_route(route: Route) -> None:
    request = route.request
    url = request.url

    if request.method == "PUT" and url.endswith("/api/v1/config"):
        body = json.loads(request.post_data or "{}")
        _config_state.update(body)
        _spotify_state["device_name"] = _config_state.get(
            "spotify_connect_name", _spotify_state["device_name"]
        )
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_config_state))
        return

    endpoints: dict[str, object] = {
        "/api/v1/health/details": _HEALTH_DETAILS,
        "/api/v1/health": _HEALTH,
        "/api/v1/config": _config_state,
        "/api/v1/spotify/restart": {},
        "/api/v1/spotify": _spotify_state,
        "/api/v1/audio": _AUDIO,
        "/api/v1/speaker": _SPEAKER,
        "/api/v1/battery": _BATTERY,
    }
    for suffix, payload in endpoints.items():
        if url.endswith(suffix):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
            return

    route.continue_()


def _pause(page: Page, seconds: float) -> None:
    page.wait_for_timeout(int(seconds * 1000))


def _natural_click(page: Page, locator: Locator) -> None:
    """Move the (visual) cursor to the element over several steps, then
    click — instead of Playwright's default instant teleport-and-click.

    Deliberately skips scroll_into_view_if_needed(): every target in this
    fixed, single-screen phone layout is already always on-screen (the one
    genuinely scrollable case is handled separately by _maybe_scroll), and
    that call's "wait for a stable frame" check is the first thing to time
    out under heavy load (e.g. while --gif's video encoding competes for the
    CPU on an underpowered / GPU-less host)."""
    box = locator.bounding_box()
    if box is None:
        locator.click()
        return
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y, steps=30)
    _pause(page, 0.15)
    page.mouse.down()
    _pause(page, 0.05)
    page.mouse.up()


def _scroll_by(page: Page, amount: float, *, steps: int = 18, step_pause: float = 0.045) -> None:
    for _ in range(steps):
        page.mouse.wheel(0, amount / steps)
        _pause(page, step_pause)


def _maybe_scroll(page: Page) -> None:
    """Scroll down and back up in small, human-paced increments — only if
    the dashboard actually overflows the viewport."""
    overflow = page.evaluate("document.documentElement.scrollHeight - window.innerHeight")
    if overflow <= 4:
        return
    _scroll_by(page, overflow, steps=18, step_pause=0.045)
    _pause(page, 0.4)
    _scroll_by(page, -overflow, steps=18, step_pause=0.035)


def _scroll_sheet_into_view(page: Page, sheet_selector: str) -> None:
    """Same idea as _maybe_scroll but for a sheet's own internal scroll
    container (max-height + overflow-y: auto), not the page — used so a
    tall settings sheet (e.g. the Danger Zone / Factory reset section,
    which sits below the fold at our deliberately short dashboard-sized
    viewport) still gets shown rather than silently clipped."""
    info = page.evaluate(
        f"""() => {{
          const el = document.querySelector({sheet_selector!r});
          return {{overflow: el.scrollHeight - el.clientHeight}};
        }}"""
    )
    overflow = info["overflow"]
    if overflow <= 4:
        return
    box = page.locator(sheet_selector).bounding_box()
    if box is None:
        return
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=15)
    _pause(page, 0.2)
    _scroll_by(page, overflow, steps=14, step_pause=0.05)
    _pause(page, 0.8)
    _scroll_by(page, -overflow, steps=14, step_pause=0.04)


def _run_demo(page: Page, base_url: str) -> None:
    # 1. Open the Dashboard
    page.goto(f"{base_url}/index.html?mock", wait_until="load")
    page.wait_for_selector(".on-scene")
    # 2. Pause for ~2 seconds
    _pause(page, 2.5)

    # 3. Slowly scroll if the page is scrollable
    _maybe_scroll(page)

    # 4. Open Settings
    _natural_click(page, page.locator('[data-action="open-settings"]'))
    page.wait_for_selector("#settings-overlay:not(.hidden)")
    # 5. Pause
    _pause(page, 1.0)

    # 5b. The settings sheet is taller than our dashboard-sized viewport —
    # scroll down within it to actually show the Danger Zone / Factory
    # reset section, then back up to where the name field lives.
    _scroll_sheet_into_view(page, "#settings-overlay .sheet")

    # 6. Change the Spotify Connect device name
    name_field = page.locator("#set-spotify-name")
    _natural_click(page, name_field)
    # Explicit select-all-then-type, rather than relying on a triple-click
    # to select the existing text: Playwright's raw mouse.down()/up() pairs
    # (used by _natural_click for human-like timing) don't reliably register
    # as a real multi-click with the browser, so the old name was never
    # selected and the new one just got appended after it.
    page.keyboard.press("ControlOrMeta+a")
    _pause(page, 0.15)
    page.keyboard.type(_NEW_DEVICE_NAME, delay=90)
    _pause(page, 0.5)

    # 7. Save
    _natural_click(page, page.locator('[data-action="save-settings"]'))
    page.wait_for_selector("#settings-overlay.hidden", state="attached")
    page.wait_for_selector("#toast:not(.hidden)")
    # 8. Return to Dashboard (saveSettings() closes the sheet itself)
    _pause(page, 1.3)

    # 9. Open Diagnostics
    _natural_click(page, page.locator('[data-action="open-health"]'))
    page.wait_for_selector("#health-overlay:not(.hidden)")
    # 10. Pause
    _pause(page, 1.8)

    # 11. Return to Dashboard
    _natural_click(page, page.locator('[data-action="close-health"]'))
    page.wait_for_selector("#health-overlay.hidden", state="attached")

    # 12. Finish on the Dashboard for a few seconds
    _pause(page, 2.0)


def _webm_to_gif(webm_path: Path, gif_path: Path, *, fps: int, width: int) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError(
            "ffmpeg is required for --gif (webm -> gif conversion) but wasn't found on PATH."
        )
    gif_path.parent.mkdir(parents=True, exist_ok=True)
    # Palette-based two-stage filter in one ffmpeg invocation — much sharper
    # than a naive conversion, at a file size still reasonable for a README.
    vf = (
        f"fps={fps},scale={width}:-1:flags=lanczos,split[s0][s1];"
        "[s0]palettegen=stats_mode=diff[p];[s1][p]paletteuse=dither=bayer"
    )
    subprocess.run(  # noqa: S603 — ffmpeg resolved via shutil.which, args are our own ints/paths
        [ffmpeg, "-y", "-i", str(webm_path), "-vf", vf, str(gif_path)],
        check=True,
        capture_output=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without a visible window (smoke-test only — there's nothing to record).",
    )
    parser.add_argument(
        "--width", type=int, default=414, help="Viewport width (default: 414, phone-sized)."
    )
    parser.add_argument(
        "--height",
        type=int,
        default=660,
        help=(
            "Viewport height (default: 660 — sized to the dashboard's actual content "
            "height, not a full 896 phone screen, so there's no dead black space below it)."
        ),
    )
    parser.add_argument(
        "--gif",
        type=Path,
        default=None,
        help=(
            "Record the run and write an optimized GIF to this path "
            "(e.g. docs/images/portal-demo.gif) — no manual screen recording needed. "
            "Requires ffmpeg."
        ),
    )
    parser.add_argument("--gif-fps", type=int, default=12, help="GIF frame rate (default: 12).")
    parser.add_argument(
        "--gif-width", type=int, default=390, help="GIF output width in pixels (default: 390)."
    )
    args = parser.parse_args()

    static_dir = _find_static_dir()
    video_dir = Path(tempfile.mkdtemp(prefix="companion-demo-")) if args.gif else None

    with _static_server(static_dir) as base_url, sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        context = browser.new_context(
            viewport={"width": args.width, "height": args.height},
            device_scale_factor=2,
            record_video_dir=video_dir,
            record_video_size={"width": args.width, "height": args.height} if video_dir else None,
        )
        context.add_init_script(_CURSOR_SCRIPT)
        page = context.new_page()
        # Generous headroom: video encoding on an underpowered/GPU-less host
        # (this is what --gif exercises) can slow down actionability checks
        # well past Playwright's normal 30s default.
        page.set_default_timeout(90_000)
        page.route("**/api/v1/**", _handle_api_route)

        _run_demo(page, base_url)

        if args.gif:
            # The video file isn't finalized/playable until the context
            # closes — must happen before we can hand it to ffmpeg.
            video = page.video
            context.close()
            assert video is not None
            webm_path = Path(video.path())
            _webm_to_gif(webm_path, args.gif, fps=args.gif_fps, width=args.gif_width)
            print(f"Wrote {args.gif}")

        browser.close()

    if video_dir is not None:
        shutil.rmtree(video_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
