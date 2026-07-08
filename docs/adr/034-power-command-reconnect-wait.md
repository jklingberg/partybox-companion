# ADR-034: Wait for Reconnect Across a Power Command's Own BLE Drop

**Status:** Accepted

---

## Context

Reported symptom: a user powered the speaker off from the Companion Portal, then immediately tried to power it back on, and the Portal reported "Speaker is not connected" — even though the speaker was on mains power and, per [ADR-033](033-speaker-standby-detection.md), was expected to stay BLE-connected (just idle) rather than actually disconnect.

Live investigation on hardware (2026-07-08) traced this to a previously undocumented behavior: sending either power command (`AA 03 01 04` off, `AA 03 01 05` on) makes the PartyBox itself drop the BLE control link roughly 1-2 seconds after receiving the command, not merely go quiet. `DeviceManager` correctly detects this as `ConnectionLostError` and reconnects automatically, but the reconnect — a fresh BLE scan plus connect, since the speaker's address rotates (`docs/reverse-engineering/open-questions.md`) — measured **~14-17 seconds** across repeated trials. `PartyBoxDevice.power.turn_on()`/`turn_off()` are pure fire-and-forget GATT writes (confirmed by inspection — no disconnect call anywhere in the SDK), so this is genuine PartyBox firmware behavior (likely a full internal reset as part of the power transition — consistent with `docs/reverse-engineering/discoveries.md`'s note that the large opcode-`0x12` state dump, tags `0x31`-`0x5b`, is only ever observed during this shutdown sequence), not something introduced by this codebase.

Before this decision, `DeviceManager.power_on()` / `power_off()` / `get_volume()` / `set_volume()` all checked `self._device is None` and raised `DeviceNotConnectedError` (surfaced as HTTP 503) immediately. Any command issued during that ~15s reconnect window — which reliably follows every power command — failed, even though the daemon was actively and successfully reconnecting in the background. In practice this meant "turn off, then turn back on" almost always failed on the first attempt from the Portal.

## Decision

`DeviceManager` now exposes `_get_connected_device()`, used by all four device-command methods (`power_on`, `power_off`, `get_volume`, `set_volume`) instead of a bare `self._device is None` check:

- If a device is already connected, return it immediately (no behavior change for the common case).
- If not, wait on a new `asyncio.Event` (`_connected_event`, set whenever `_connect_and_maintain` lands a connection, cleared on every disconnect) for up to `_RECONNECT_WAIT_TIMEOUT = 20.0` seconds — comfortably above the observed ~14-17s reconnect — before raising `DeviceNotConnectedError`.

This makes the command wait out the reconnect the speaker itself triggers, rather than requiring the Portal or the user to retry. No client-side fetch timeout exists in the Portal's `get()` helper (confirmed in `webui/static/index.html`), so the extra latency surfaces as the existing busy/spinner state on the power button, not a broken request.

**Validated on hardware:** `power/off` followed immediately by `power/on` now blocks for ~14s (matching the reconnect) and returns `204`, with the speaker actually powering back on (`audio_ready` and `speaker_state` both converge to reflect it) — versus an instant `503` before this change.

**Portal-side follow-up (same root cause).** Fixing the daemon exposed a second-order UI bug: `GET /api/v1/health` legitimately reports `speaker_state: "off"` for the ~1-2s the real disconnect is in progress (before the daemon's own reconnect completes), and the Portal's `deriveScene()` (`webui/static/index.html`) mapped that straight to `Scene.OFF` — the "Companion can't reach the speaker, plug it in" screen. Visually indistinguishable from a genuine unplug, this fired on every power command and, confirmed via live Playwright screenshots against `partybox.local`, produced a confusing flash into the "unplugged" copy before settling. Fixed by adding client-side state `S.powerPending` (set the instant a power command is sent, cleared either on request failure or by a 22s safety timer — not reactively on the first "not off" reading, since the `off` command's own request resolves before the real disconnect even starts) and a new `Scene.POWERING`, so `deriveScene()` shows a neutral "Turning speaker off/on… this can take up to 20 seconds" screen instead of the alarming plug icon for this specific, expected, self-inflicted gap. Genuine disconnects (no power command in flight) are unaffected and still show `Scene.OFF`.

**AudioService follow-up (same root cause, a third symptom).** Watching the fixed Portal live surfaced one more stale-status case: the "Bluetooth Audio" row kept showing green "connected" for a while after the speaker visibly entered standby, only flipping to "Connecting…" later. `AudioService` (the A2DP/Bluetooth-Classic link, [ADR-028](028-audio-readiness-model.md)) is a separate Bluetooth subsystem from the BLE control link this ADR is about, and only re-checks its own connection every `_CHECK_INTERVAL` (60s) while it believes it's still connected — a deliberate ADR-028 choice for what was assumed to be an otherwise-idle link. In practice the audio link tends to drop at the same time the control link does, so that 60s check interval reads as a stale "connected" status for however much of the window is left. Fixed by adding `AudioService.recheck_now()` (sets the same `_reconnect_now` interrupt `update_address()`/`forget()` already use, now also checked by the connected-idle wait, not just the retry-backoff wait) and a new supervised coroutine, `_recheck_audio_on_standby` (`companion/__main__.py`), that subscribes to `DeviceManager`'s bus and calls it on any `SpeakerStateChangedEvent` where the new state isn't `"on"`. Validated on hardware: `audio.connected` now flips to `false` within a few seconds of a power-off, not up to 60s later.

## Consequences

**Benefits:**
- "Turn off, then immediately turn back on" now works from the Portal without a manual retry — the reported bug's actual repro path.
- The fix is general: any of the four device commands issued during a power-command-induced reconnect (or any other transient BLE drop) now waits instead of failing, with no special-casing per command.
- No change to behavior when the speaker is genuinely and durably disconnected (unplugged, out of range) — the timeout still applies and 503 is still the eventual result, just ~20s later than before instead of instantly.

**Accepted trade-offs:**
- `POST /api/v1/power/on` (and the other three endpoints) can now legitimately take up to 20 seconds to respond when issued right after a power command, instead of resolving in milliseconds. This is a deliberate trade: correctness (the command actually lands) over responsiveness, for an operation the user only issues occasionally.
- If the speaker is truly gone, callers now wait the full 20s before getting the 503 they used to get instantly. Considered acceptable — a real absence is not the common case this endpoint is optimized for, and 20s is still well within reasonable HTTP client patience.
- Adds one more piece of asyncio-coordinated state (`_connected_event`) that must stay in lockstep with `self._device` at every assignment site (`_connect_and_maintain`'s connect and its `finally`, and `_disconnect()`). Missing a site would reintroduce a silent instant-503 regression or an unbounded false wait; this is a maintenance burden worth flagging for reviewers of future changes to the connect/disconnect paths.

## Rejected alternatives

- **Fix it in the Portal (client-side retry)**: rejected as the primary fix — it would need to be duplicated in every client (Portal, Home Assistant, scripts hitting the REST API directly per [ADR-012](012-interoperability-positioning.md)'s interoperability stance), whereas the daemon can absorb the wait once for everyone.
- **Make the SDK's `PowerCapability` avoid triggering the reconnect** — not viable: the disconnect is the speaker's own behavior in response to the power command, not something the write call can suppress.
- **Treat this as confirmation to re-open ADR-033's standby model** — considered and rejected: this is a separate phenomenon (an explicit-command-triggered BLE reset) from auto-idle standby (which does keep the BLE link up, as ADR-033 validated). Conflating the two would have led to changing the wrong code path.
- **Make `power_off()` symmetric with `power_on()` by blocking on its own subsequent reconnect** — proposed during PR review and rejected: `power_off()`'s write already succeeds before the speaker's self-triggered BLE drop even begins, so blocking on and reporting the *outcome* of that later reconnect would report a successfully-executed command as an API failure whenever reconnect is merely slow. This is the command-outcome-vs-transport-recovery conflation formalized as Rule 2 in [ADR-035](035-state-ownership-and-signal-pipeline.md).

Related: [ADR-033](033-speaker-standby-detection.md) (the auto-idle standby model this behavior is easily confused with, but is not), [ADR-032](032-capability-probing.md), [ADR-028](028-audio-readiness-model.md) (the A2DP health-check cadence the AudioService follow-up above reacts against), [ADR-035](035-state-ownership-and-signal-pipeline.md) (the general state-ownership rules this ADR's `power_on()`/`power_off()` asymmetry is an instance of).
