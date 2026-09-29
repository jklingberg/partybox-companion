"""Deterministic, headed Playwright walkthrough of the Companion Portal.

This does not test the app — it drives a real browser through a fixed,
human-paced sequence of interactions against the Portal's built-in `?mock`
demo mode, so the result can be screen-recorded into a README GIF. No
PartyBox hardware or running daemon is required.

Three chapters, one continuous recording: Wi-Fi captive-portal setup
(`?mock&provision`), speaker pairing (`?mock&state=pair`), then the
dashboard/Settings/Diagnostics tour (`?mock`). Each chapter is a fresh
navigation of the same page/context, so Playwright's video recorder — which
records per context, not per navigation — captures all three back to back
without a cut.

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

# Onboarding chapter fixtures (Wi-Fi provisioning + speaker pairing) — as
# fake/cosmetic as _NEW_DEVICE_NAME above. Nothing here reaches a real
# network, GitHub, or speaker; see the new route branches in
# _handle_api_route and the manual handlePairingProgress() calls in
# _run_pairing_chapter.
_WIFI_PASSWORD = "hunter2-guestnet"  # noqa: S105 — fake, never sent to a real network
_GITHUB_USER = "demo-user"
_GITHUB_IMPORTED_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDemoOnlyNotARealKeyForTheReadmeGif demo@laptop"
)

# Height, in CSS px, reserved at the top of every recorded frame for the
# fake browser-chrome bar below — added on top of the requested viewport
# height (see main()) so the app itself still gets its full, undiminished
# height beneath it.
_CHROME_BAR_HEIGHT = 34

# Persistent "browser chrome": a small address-bar pill reading
# partybox.local, fixed at the very top of every frame for the whole
# recording — headless capture has no real OS/browser window to show one,
# so this fakes just enough of it to frame the walkthrough as "a page open
# at the appliance's own address" throughout, not just a bare app view.
#
# Also draws a dot that tracks real cursor position via `mousemove`.
# Playwright's synthetic mouse events don't reliably paint an OS cursor
# sprite across platforms, so we draw our own — purely cosmetic, doesn't
# touch app state.
#
# Also hides the "MOCK MODE" banner: it's correct and useful during UI
# development, but it would give the game away in a README GIF meant to
# look like a real, healthy appliance.
_CHROME_SCRIPT = (
    """
(() => {
  const BAR_HEIGHT = """
    + str(_CHROME_BAR_HEIGHT)
    + """;
  // add_init_script fires as early as "document created" — sometimes before
  // document.documentElement/head/body exist yet — so every DOM touch here
  // is guarded, and setup runs both immediately and again on
  // DOMContentLoaded to cover whichever moment actually has a usable DOM.
  function injectStyle() {
    const root = document.head || document.documentElement;
    if (!root || root.querySelector('#__demo_style')) return;
    const style = document.createElement('style');
    style.id = '__demo_style';
    style.textContent = `
      #mock-banner { display: none !important; }
      /* Reserve BAR_HEIGHT at the top of every scene for the address bar
         below, without shrinking the app's own content area: each of these
         is a min-height:100dvh floor, so the recorded viewport is enlarged
         by BAR_HEIGHT (see main()) and this override gives that extra
         space back to the reserved top padding instead of the scene. */
      .scene, .center-scene, .on-scene { min-height: calc(100dvh - ${BAR_HEIGHT}px) !important; }
      body { padding-top: ${BAR_HEIGHT}px; }
    `;
    root.appendChild(style);
  }
  function attachChrome() {
    if (!document.body || document.getElementById('__demo_addressbar')) return;
    const bar = document.createElement('div');
    bar.id = '__demo_addressbar';
    bar.textContent = '\\u{1F512} partybox.local';
    Object.assign(bar.style, {
      position: 'fixed', top: '0', left: '0', right: '0', height: BAR_HEIGHT + 'px',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      background: 'rgba(28,28,30,0.92)', color: 'rgba(255,255,255,0.92)',
      fontFamily: '-apple-system,BlinkMacSystemFont,sans-serif',
      fontSize: '13px', fontWeight: '500',
      zIndex: 2147483646, boxShadow: '0 1px 3px rgba(0,0,0,0.3)',
    });
    document.body.appendChild(bar);

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
  const ready = () => { injectStyle(); attachChrome(); };
  document.addEventListener('DOMContentLoaded', ready);
  ready();
  window.addEventListener('mousemove', (e) => {
    const dot = document.getElementById('__demo_cursor');
    if (dot) { dot.style.left = e.clientX + 'px'; dot.style.top = e.clientY + 'px'; }
  });
})();
"""
)


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

    if request.method == "POST" and url.endswith("/api/v1/audio/pair"):
        # startPairing() only checks the status code, not the body — see
        # _run_pairing_chapter, which drives the rest of the flow by hand.
        route.fulfill(status=200, content_type="application/json", body="{}")
        return

    if request.method == "POST" and url.endswith("/api/v1/ssh/github-import"):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"keys": [_GITHUB_IMPORTED_KEY]}),
        )
        return

    if request.method == "PUT" and url.endswith("/api/v1/ssh/settings"):
        body = json.loads(request.post_data or "{}")
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "enabled": bool(body.get("authorized_keys")),
                    "has_key": bool(body.get("authorized_keys")),
                    "authorized_keys": body.get("authorized_keys", []),
                    "applied_at": "2026-07-28T12:00:00Z",
                    "error": None,
                    "confirmed": True,
                }
            ),
        )
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


def _slow_scroll_sheet_to(page: Page, sheet_selector: str, target_selector: str) -> None:
    """Scroll the settings sheet's own internal scroll container (max-height
    + overflow-y: auto, not the page) down to *target_selector*, in small,
    human-paced increments — long enough for a viewer to actually read the
    fields passing by, rather than an instant `scrollIntoView` jump straight
    to it."""
    box = page.locator(sheet_selector).bounding_box()
    if box is None:
        return
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=15)
    _pause(page, 0.2)
    delta = page.evaluate(
        f"""() => {{
          const sheet = document.querySelector({sheet_selector!r});
          const target = document.querySelector({target_selector!r});
          return target.getBoundingClientRect().top - sheet.getBoundingClientRect().top - 24;
        }}"""
    )
    if delta <= 4:
        return
    _scroll_by(page, delta, steps=26, step_pause=0.07)


def _run_wifi_chapter(page: Page, base_url: str) -> None:
    """Captive-portal Wi-Fi setup: scan (pre-seeded), pick a secured
    network, enter its password, connect. `connectWifi()` does fire a real
    fetch()/poll against /api/v1/wifi/connect and /api/v1/wifi/status, but
    those are deliberately left unmocked (see module docstring) — both 404
    against the demo's static file server, which connectWifi() already
    tolerates (it only logs and keeps polling), so the "Connecting…"
    message stays on screen right up until we navigate away to the next
    chapter, with no error flash and no need to fake the connect/poll
    round trip."""
    page.goto(f"{base_url}/index.html?mock&provision", wait_until="load")
    page.wait_for_selector(".net-row")
    _pause(page, 1.8)

    _natural_click(page, page.locator(".net-row").first)
    page.wait_for_selector("#pw-field:not(.hidden)")
    _pause(page, 0.4)

    _natural_click(page, page.locator("#wifi-pw"))
    page.keyboard.type(_WIFI_PASSWORD, delay=70)
    _pause(page, 0.5)

    _natural_click(page, page.locator('[data-action="connect-wifi"]'))
    page.wait_for_selector("#prov-msg:not(:empty)")
    _pause(page, 2.2)


def _run_pairing_chapter(page: Page, base_url: str) -> None:
    """Speaker pairing scene. startPairing() itself is real (POST
    /api/v1/audio/pair, mocked above to 200), but its progress normally
    streams in over the WebSocket from a real daemon — there is none here,
    so the "paired" / "connected" beats are played by hand via
    handlePairingProgress(), the same function the real WS-event handler
    calls. That function is a plain top-level declaration in the Portal's
    classic (non-module) script, so it — and the `S` state object it
    reads — are reachable directly by name from page.evaluate(), which
    runs in the same page realm."""
    page.goto(f"{base_url}/index.html?mock&state=pair", wait_until="load")
    page.wait_for_selector('[data-action="start-pairing"]')
    _pause(page, 1.4)

    _natural_click(page, page.locator('[data-action="start-pairing"]'))
    _pause(page, 1.8)

    page.evaluate(
        "S.audio = {connected: false, address: 'AA:BB:CC:DD:EE:FF', pairing_state: 'idle'};"
        "handlePairingProgress();"
    )
    _pause(page, 1.3)

    page.evaluate("S.audio.connected = true; handlePairingProgress();")
    page.wait_for_selector("#toast:not(.hidden)")
    _pause(page, 1.8)

    # Hand off to the Dashboard in-page rather than a fresh page.goto(): the
    # real app reaches Scene.ON the same way, via a state mutation plus
    # re-render off a WS event, never a reload. A goto() here would repaint
    # from a blank frame first, which reads as an unexplained flicker right
    # after "Paired and connected!".
    page.evaluate("S.scene = deriveScene(); render();")
    page.wait_for_selector(".on-scene")
    _pause(page, 2.0)


def _open_dashboard(page: Page, base_url: str) -> None:
    """Fresh navigation straight to the Dashboard — only used with
    --skip-onboarding. The onboarding path instead hands off in-page from
    the pairing chapter (see the tail of _run_pairing_chapter) to avoid a
    reload flicker."""
    page.goto(f"{base_url}/index.html?mock", wait_until="load")
    page.wait_for_selector(".on-scene")
    _pause(page, 2.5)


def _run_demo(page: Page, base_url: str) -> None:
    # 1. Slowly scroll if the page is scrollable
    _maybe_scroll(page)

    # 2. Open Settings
    _natural_click(page, page.locator('[data-action="open-settings"]'))
    page.wait_for_selector("#settings-overlay:not(.hidden)")
    # 3. Pause
    _pause(page, 1.0)

    # 4. Change the Spotify Connect device name
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
    _pause(page, 0.6)

    # 5. Slowly scroll down to the new SSH access section — giving the
    # viewer time to actually read the Spotify fields passing by — then
    # import a key from GitHub, the newest addition to the Settings sheet.
    _slow_scroll_sheet_to(page, "#settings-overlay .sheet", "#ssh-section")
    _pause(page, 0.6)
    _natural_click(page, page.locator("#ssh-github-user"))
    page.keyboard.type(_GITHUB_USER, delay=80)
    _pause(page, 0.3)
    _natural_click(page, page.locator('[data-action="ssh-github-import"]'))
    page.wait_for_selector("#ssh-msg:not(:empty)")
    _pause(page, 1.4)

    # 6. Slowly scroll the rest of the way down to Save, reading the SSH
    # section's own fields on the way, then click it.
    save_btn = page.locator('[data-action="save-settings"]')
    _slow_scroll_sheet_to(page, "#settings-overlay .sheet", '[data-action="save-settings"]')
    _pause(page, 0.4)
    _natural_click(page, save_btn)
    page.wait_for_selector("#settings-overlay.hidden", state="attached")
    page.wait_for_selector("#toast:not(.hidden)")
    # 7. Return to Dashboard (saveSettings() closes the sheet itself)
    _pause(page, 1.3)

    # 8. Open Diagnostics
    _natural_click(page, page.locator('[data-action="open-health"]'))
    page.wait_for_selector("#health-overlay:not(.hidden)")
    # 9. Pause
    _pause(page, 1.8)

    # 10. Return to Dashboard
    _natural_click(page, page.locator('[data-action="close-health"]'))
    page.wait_for_selector("#health-overlay.hidden", state="attached")

    # 11. Finish on the Dashboard for a few seconds
    _pause(page, 2.0)


def _webm_to_gif(webm_path: Path, gif_path: Path, *, fps: int, width: int) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError(
            "ffmpeg is required for --gif (webm -> gif conversion) but wasn't found on PATH."
        )
    gif_path.parent.mkdir(parents=True, exist_ok=True)
    # Palette-based two-stage filter in one ffmpeg invocation. dither=none is
    # deliberate, not a quality shortcut — this is flat-color UI (not a
    # photo), and ordered/error-diffusion dithering scatters per-pixel noise
    # that defeats GIF's run-length-friendly LZW compression: measured ~3.8x
    # smaller with dither=none than dither=bayer on this exact recording,
    # with no visible banding since there's little continuous gradient to
    # dither in the first place.
    vf = (
        f"fps={fps},scale={width}:-1:flags=lanczos,split[s0][s1];"
        "[s0]palettegen=stats_mode=diff[p];[s1][p]paletteuse=dither=none"
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
    parser.add_argument(
        "--skip-onboarding",
        action="store_true",
        help="Skip the Wi-Fi setup and pairing chapters, starting directly on the Dashboard.",
    )
    args = parser.parse_args()

    static_dir = _find_static_dir()
    video_dir = Path(tempfile.mkdtemp(prefix="companion-demo-")) if args.gif else None

    with _static_server(static_dir) as base_url, sync_playwright() as p:
        # The recorded viewport is taller than --height by the chrome bar's
        # height — the bar occupies that extra strip at the top, and the
        # CSS injected by _CHROME_SCRIPT gives the rest back to the app so
        # its own content still renders at exactly --height, undiminished.
        browser_height = args.height + _CHROME_BAR_HEIGHT
        browser = p.chromium.launch(headless=args.headless)
        context = browser.new_context(
            viewport={"width": args.width, "height": browser_height},
            device_scale_factor=2,
            record_video_dir=video_dir,
            record_video_size={"width": args.width, "height": browser_height}
            if video_dir
            else None,
        )
        context.add_init_script(_CHROME_SCRIPT)
        page = context.new_page()
        # Generous headroom: video encoding on an underpowered/GPU-less host
        # (this is what --gif exercises) can slow down actionability checks
        # well past Playwright's normal 30s default.
        page.set_default_timeout(90_000)
        page.route("**/api/v1/**", _handle_api_route)

        if not args.skip_onboarding:
            _run_wifi_chapter(page, base_url)
            _run_pairing_chapter(page, base_url)
        else:
            _open_dashboard(page, base_url)
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
