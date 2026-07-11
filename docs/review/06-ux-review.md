# 06 — UX Review

The Portal (Ember redesign, `webui/static/index.html`) is genuinely good: one
scene at a time, derived-not-stored state, self-hosted fonts so it renders on
the provisioning AP, real accessibility affordances (ARIA live regions, roles),
and honest copy that doesn't blame the user. The scene model is a better
foundation than most commercial appliance UIs. Findings below are ordered by
user impact; the first two are **real bugs**, not preferences.

---

### UX-01 — Power-toggle label/state uses battery presence as a proxy for awake, mislabeling mains-powered speakers
**Severity:** P1 (bug) · **Status:** OPEN · **Where:** `webui/static/index.html::audioAwake()` and `patchOnScene`

```js
function audioAwake() { return S.battery && typeof S.battery.level === 'number'; }
```

`power-toggle` decides on/off via `audioAwake()`, which is true only when a
battery reading exists. This is exactly the ADR-033 mistake (inferring power
state from battery presence) that the *daemon* was fixed to stop making — and
it was reintroduced client-side. Consequences:

- On a **mains-powered speaker with no battery** (e.g. a stationary PartyBox
  model, or the 520 when `battery` is null), `audioAwake()` is always false, so
  the toggle always sends `power-on` even when the speaker is on. The user taps
  "Turn off" (the ON scene's static label) and it powers *on* / no-ops.
- The daemon already computes the authoritative signal: `health.speaker_state`
  (`on`/`standby`/`off`). The Portal should use it — this is an ADR-035 Rule 1
  violation (re-deriving a fact the daemon owns) sitting in the shipped code,
  the same class ADR-035 was written to prevent.

**Fix:** `power-toggle` should send `off` when `S.health.speaker_state === 'on'`,
`on` otherwise. Delete `audioAwake()`. The ON scene is only reached when
`speaker_state === 'on'` (per `deriveScene`), so the label "Turn off" is
correct there; the toggle just needs to read the real state, not the battery.

### UX-02 — The ON scene renders whenever the speaker is awake, even if audio can't play
**Severity:** P2 (bug-ish) · **Status:** OPEN · **Where:** `deriveScene()` precedence

`deriveScene` returns `ON` when `speaker_state === 'on'` and an audio address
exists — but ON is reached **before** checking `audio.connected`. So a speaker
that is on and paired but whose A2DP link is down (WirePlumber degraded,
`profile-unavailable`, flap cooldown) shows the full ON scene with "Ready to
play." The hero ring shows `state-pending` / "Connecting audio…" (good, that
part reacts to `audio.connected`), but the top-level framing says everything's
fine. During the exact failure modes ADR-028 spent pages on, the user is told
"Ready to play" and gets silence. The health strip will eventually show it, but
the primary message contradicts reality.

**Fix:** either a dedicated "audio not ready" treatment when
`speaker_state==='on' && !audio.connected` lasts more than a few seconds, or at
minimum make the hero caption authoritative over the "Ready to play" default.

### UX-03 — First-time users hit pairing with no idea the physical button is mandatory *before* they start
**Severity:** P2 · **Status:** OPEN · **Where:** PAIR scene copy, ADR-027 (button-gated pairing window is unavoidable)

The pairing window is short and button-gated (ADR-027) — the user *must* press
the speaker's Bluetooth button within seconds of tapping "Start Pairing," or it
fails. The PAIR copy explains this, but the failure recovery loop is punishing:
if they tap Start first (natural), scan runs 60 s, fails, and they retry. The
open-questions doc confirms pairing mode can't be triggered over BLE, so this
friction is intrinsic — which means the *copy and sequencing* have to carry it.

**Recommendation:** invert the instruction emphasis — make "1. Press and hold
the Bluetooth button until the LEDs flash" a numbered, unmissable step
**above** the button, and ideally disable "Start Pairing" behind a "I've put my
speaker in pairing mode" checkbox so the two actions are temporally coupled.
Consider a shorter first scan with a "still looking… press the button now"
nudge at ~10 s rather than a silent 60 s wait.

### UX-04 — "Ember" copy assumes a battery model in places; verify mains-only wording
**Severity:** P3 · **Status:** OPEN

Standby scene: "The speaker is plugged in but powered down" — fine. But battery
chip, remaining-playtime, and several flows assume battery data exists. On a
mains-only model much of the status surface is empty. Not broken, but the UI
was clearly designed and mocked against the battery-having 520
(`MOCK_BATTERY`). Walk the mains-only path (the daemon supports it:
`battery: null`) and confirm nothing reads as "missing" rather than "n/a."

### UX-05 — WebSocket API key travels in the URL query string
**Severity:** P3 (security-adjacent UX) · **Status:** OPEN · **Where:** `connectWS()`, `api/ws.py`

`ws://host/api/v1/events?api_key=…` puts the key in a URL — logged by any proxy,
stored in browser history, visible in referrer contexts. Low severity on a LAN
appliance but a known anti-pattern. If auth becomes real (SEC-02), prefer a
short-lived token or a cookie/subprotocol. Note in the ADR-036 lineage.

### UX-06 — No feedback that factory reset / power actions are irreversible-ish before they fire
**Severity:** P3 · **Status:** PARTIAL · **Where:** factory-reset flow (good), power (fine)

Factory reset has a typed-`RESET` confirmation — excellent, and correctly
in-Portal (no browser chrome). Good as-is. Flagged only to note the *positive*:
this is the right pattern; apply the same "type to confirm" bar to any future
destructive action (e.g. "forget WiFi" when DEBT/roadmap adds it, which drops
the appliance off-network mid-request per ADR-031's own caveat — that one needs
the "you'll need to reconnect to the setup network" pre-warning ADR-031
describes but PR #47 deferred).

### UX-07 — Health strip collapses multiple issues to a count; the one actionable item can hide
**Severity:** P3 · **Status:** OPEN · **Where:** `patchOnScene` health strip

With ≥2 issues the strip shows "N issues need attention" and the user must open
the sheet. Fine, but if one of those issues has a one-tap fix (Pair, Restart
Spotify) and another is cosmetic, the actionable one is buried behind a tap.
Minor. Consider surfacing the highest-actionability issue inline.

---

## What the UX gets right (keep these)

- **One scene, derived state.** The single strongest UI decision; it makes the
  whole thing predictable and is why UX-01/02 are the *only* two state bugs
  rather than dozens.
- **Honest, non-blaming copy.** "Companion can't reach the speaker" not "Error."
- **Self-contained, no CDN.** Required for AP-mode rendering; correctly executed.
- **Push-based updates** (ADR-036) make it feel live without hammering the Pi
  (mostly — see DEBT-12 for the BLE-event fan-out exception).
- **Accessibility basics present** (roles, `aria-live`, focus management on
  modals). Rare at this stage. Don't regress it.
- **Powering scene** (ADR-034) — turning a confusing 20 s gap into explained
  waiting is exactly the right instinct.
