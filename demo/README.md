# Companion Portal demo

A deterministic, headed Playwright walkthrough of the Companion Portal, built for
recording the README GIF/screenshot — not for testing the app.

It serves the Portal's static files locally, opens it in `?mock` mode (the same
built-in demo mode used for UI development — no daemon, no PartyBox hardware),
and drives a fixed, human-paced sequence of interactions: dashboard → settings
(rename the device, save) → diagnostics → back to dashboard. Every run produces
the same sequence in the same timing — nothing here polls, randomizes, or races
a real backend.

This is a standalone project on purpose: Playwright and its browser binary are a
few hundred MB and have nothing to do with running the appliance, so they live
outside the `packages/*` workspace and are never pulled into a Pi image build.

## Setup

Run this on a machine with a display (your laptop) — not inside a headless
devcontainer, since the whole point is a visible browser window to screen-record.

```bash
cd demo
uv sync
uv run playwright install chromium
```

## Run

```bash
uv run companion-demo
```

A Chromium window opens at phone size and runs through the ~14 second sequence
listed above, then closes. Start your screen recording (macOS: `Cmd+Shift+5`)
right before running the command — the initial 2.5s pause on the dashboard gives
you a buffer to get the recording started.

### Or skip screen recording entirely

Playwright can record the run directly and hand it straight to `ffmpeg` for an
optimized GIF — no manual screen recording, no trimming, byte-identical output
every run:

```bash
uv run companion-demo --gif ../docs/images/portal-demo.gif
```

Requires `ffmpeg` on `PATH` (`brew install ffmpeg` / `apt install ffmpeg`).
Works headless too (`--headless --gif ...`), which is how this was produced and
verified in CI/sandboxes with no display at all.

Useful flags:

```bash
uv run companion-demo --headless        # smoke-test the sequence, no window
uv run companion-demo --width 390 --height 844   # different phone size
uv run companion-demo --gif out.gif --gif-fps 15 --gif-width 320
```

## From the repo root

```bash
make demo
```
