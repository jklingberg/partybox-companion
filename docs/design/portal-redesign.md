# Companion Portal Redesign — "Ember"

Status: **in progress** — steps 1–2 of §13 shipped 2026-07-06 (commits
`2eb3342`, and the `speaker_state` follow-up); steps 3–4 remain.
Author: UX redesign pass, 2026-07-06

A ground-up redesign of the Companion Portal. Goal: the Portal should feel
like the control surface of a premium consumer audio appliance — not a
Raspberry Pi web application. Design driver:

> The page **is** the status. The device state decides what exists on screen,
> not what a table row says.

---

## 1. Critique of the current Portal

The current UI ([index.html](../../packages/companion/src/companion/webui/static/index.html))
is a competent admin dashboard — which is exactly the problem.

**It's a status table, not an appliance.** Five uniform rows (Speaker,
Bluetooth Audio, Spotify Connect, AirPlay, System) with colored dots and a
monospace detail column read like `systemctl status`. Nothing on screen has
hierarchy; "Speaker is off" and "System v0.1.0" get identical visual weight.

**It violates its own goals today:**

- Emoji everywhere: `🔊` as the logo, `🔋 ⚡` in battery text, `🔒` in the
  WiFi list.
- Dead UI: a permanently dimmed "AirPlay — Coming soon" row (a placeholder),
  and a "Diagnostics" footer button whose only behavior is a toast saying
  diagnostics don't exist. A button that does nothing is worse than no button.
- Raw values users can't act on, in prime screen space: two Bluetooth MAC
  addresses, a firmware string as the Speaker row's main "detail".
- "About" is a toast. Version info deserves one quiet line, not a popup.

**Redundant state.** Speaker connectivity is expressed in at least three
places at once: the header health pill ("Ready" / "Connecting audio…"), the
Speaker row, and the Bluetooth Audio row. When they disagree mid-transition,
the user sees contradiction.

**No device-state awareness.** The frontend already *derives* awake/standby
(battery reading = awake, cleared reading = standby) but only uses it to swap
two tiny table-row buttons. When the speaker is unplugged, the full dashboard
still renders — five rows of mostly-dashes. The single most important action
("turn the speaker on") is a 28px-tall `btn-sm` in a grid cell.

**Staleness by design.** Three independent polling loops (spotify 15s,
battery 20s, pairing 2s) plus a WebSocket, each mutating its own row. Between
polls, values can be wrong with no visual indication. A disconnected speaker
shows "Reconnecting automatically" forever with no escalation.

**Visually generic.** The palette is GitHub-dark (the `--ok` green `#3fb950`
is literally GitHub's), the logo is an emoji, there is no favicon, no
gradient, no light, no atmosphere. Nothing says *music*.

What's worth keeping: the zero-build single-file architecture, the
provisioning flow's structure, the pairing flow's guided copy, and the
`setMode()` single-point-of-truth idea — the redesign generalizes exactly
that idea into a full state machine.

---

## 2. The state model (foundation of the whole IA)

The Portal becomes a state machine. **Exactly one scene is on screen at any
time.** No scene ever renders another scene's content, so redundancy and
"empty card" states become structurally impossible.

```
SETUP        wifi.state ∈ {unprovisioned, ap_active, connecting}
  ↓
PAIR         audio.address == null            (no speaker ever paired)
  ↓
OFF          health.ble_connected == false    (speaker unplugged / unreachable)
  ↓
STANDBY      ble_connected && battery unavailable   (plugged in, powered off)
  ↓
ON           ble_connected && battery answering     (the actual Portal)
```

Plus one overlay state: `PORTAL_UNREACHABLE` (fetch/WS to the appliance
itself fails) — distinct from OFF, because "Companion is down" and "speaker
is unplugged" need different messages.

### Backend prerequisite: make power state explicit

Today the awake/standby distinction lives in frontend comments as "a live
battery reading is our proxy for on". That inference should move into the
daemon, which already owns the snapshot:

```
GET /api/v1/health  →  { ..., speaker_state: "off" | "standby" | "on" }
WS event            →  { type: "speaker_state_changed", state: "standby" }
```

One source of truth, computed where the data lives, and the Portal can never
show a state the daemon doesn't believe. This also fixes the current
ambiguity where battery `404` means both "standby" and "this model has no
battery". The frontend keeps a fallback inference only until this ships.

The Supervisor already produces `TaskHealth` snapshots
(`supervisor.py: Supervisor.health()`, marked "intended for Portal
diagnostics") — expose them as `GET /api/v1/health/details` for the health
sheet (§ 4.4).

---

## 3. Information architecture

Every displayed value must pass: *does the user need this to operate the
speaker?* The entire IA:

```
Scene (one of):
  SETUP     — WiFi picker (existing flow, restyled)
  PAIR      — guided one-button pairing
  OFF       — red scene: "plug the speaker in"
  STANDBY   — amber scene: one button, "Turn speaker on"
  ON        — the Portal:
                Hero        speaker name · state ring · power · battery
                Source      "Open Spotify and pick ‘<name>’" / Playing
                Health      one collapsed line → expandable sheet
Sheets (modal, on demand):
  Settings  — names, audio quality, danger zone, version line
  Health    — per-component status, debug bundle download
```

That is the complete product. No tabs, no pages, no footer.

---

## 4. Scenes

### 4.1 OFF — deep red

Full-viewport scene. Background: near-black with a deep-red radial wash and a
dim, *unlit* ring graphic (the speaker's "LED ring", off).

```
┌──────────────────────────────────┐
│                                  │
│            ( dim ring )          │
│                                  │
│        Speaker is powered off    │
│                                  │
│   Companion can't reach the      │
│   speaker. Plug it into power    │
│   and switch it on — it will     │
│   reconnect automatically.       │
│                                  │
└──────────────────────────────────┘
```

Nothing else. No diagnostics, no cards, no header chrome beyond the wordmark.
The scene quietly listens (WS + health poll) and crossfades to STANDBY/ON the
moment the speaker appears.

### 4.2 STANDBY — amber

Same composition, amber wash, ring faintly breathing. One primary action.

```
┌──────────────────────────────────┐
│                                  │
│        ( breathing ring )        │
│                                  │
│         Speaker is asleep        │
│                                  │
│        ┌────────────────┐        │
│        │  Turn speaker  │        │
│        │       on       │        │
│        └────────────────┘        │
│                                  │
└──────────────────────────────────┘
```

On tap: button enters a pending state, scene transitions when the daemon
reports the speaker awake (WS `power_changed` / state change), not
optimistically on the 204.

### 4.3 ON — the Portal

Normal (ember) theme. Single column, mobile-first.

```
┌──────────────────────────────────┐
│  ◉ Living Room              ⚙   │   header: name + settings only
├──────────────────────────────────┤
│                                  │
│         (  glowing ring  )       │   hero: state ring, breathing
│            ▶ Playing             │   or "Ready to play"
│                                  │
│      ⏻ Turn off    ▮ 87% ⚡      │   power + battery (icon, not emoji)
│                                  │
├──────────────────────────────────┤
│  Spotify Connect                 │
│  Open Spotify and choose         │   or, when active:
│  “Living Room” as the device.    │   "Playing via Spotify Connect"
├──────────────────────────────────┤
│  ● All systems ready          ›  │   health strip, one line
└──────────────────────────────────┘
```

- **Hero** answers "can I play music?" in one second: ring glowing =
  yes. Battery appears only when the model reports one; shows charge +
  source icon (plug/battery from Lucide), nothing more.
- **Source card** is instructional, not statistical: it tells the user what
  to do in the Spotify app. `running/active/device_name` map to exactly three
  renderings: instruction (ready), "Playing" (active), and an inline problem
  state ("Spotify Connect isn't running — Restart" button) when stopped.
- **AirPlay does not appear** until it exists.

### 4.4 Health strip → health sheet

Apple-System-Status model. Healthy systems occupy one line:

```
●  All systems ready                              ›
```

Any degradation promotes the strip to amber/red with the failing component
named ("● Spotify Connect stopped ›"). Tapping opens the sheet:

```
Speaker        ✓ Connected
Bluetooth      ✓ Audio ready
Spotify        ✓ Ready
Companion      ✓ Running · v0.3.1
                       [ Download debug bundle ]
```

Rows expand only when unhealthy, revealing detail + one recovery action
(Restart Spotify, Pair again, …). Firmware version, MAC address, battery
health/cycle count live here — **only** inside an unhealthy/expanded row or
the bundle, never on the main screen.

### 4.5 SETUP and PAIR

Keep the existing flows' logic; restyle as full-viewport scenes in the same
visual language (dark, single card, heat accent). PAIR replaces the current
"panel appended below the table": it's a scene, because an unpaired appliance
has nothing else to show. Copy stays task-first ("Hold the Bluetooth button
on the speaker until the LEDs flash…"), with the live progress line driven by
`pairing_state`.

---

## 5. Navigation

Removed. One screen, two sheets (Settings, Health), both dismissible. The
settings gear is the only persistent chrome besides the device name.

---

## 6. Component hierarchy

Vanilla JS, but structured (~10 small render functions keyed off one state
object — an evolution of today's `setMode`):

```
App                  state store + WS + scene router (single render path)
├─ Scene: Setup      network list, password, progress
├─ Scene: Pair       instruction, start button, progress
├─ Scene: Off        message only
├─ Scene: Standby    power-on action
└─ Scene: On
   ├─ Header         name + settings trigger
   ├─ Hero           ring, playback word, power, battery
   ├─ SourceCard     spotify instruction / playing / problem+action
   └─ HealthStrip    one line → HealthSheet
Sheets: Settings, Health        (modal, focus-trapped)
Toast                           (confirmations only, never information)
```

Rule: components never fetch. `App` owns all IO and derives one immutable
view-state per update; renders are pure functions of it. This is what makes
"never display stale information" enforceable — each datum in view-state is
`{value, fresh: bool}` and renderers must show a skeleton/unavailable
treatment when not fresh.

---

## 7. Visual identity

### 7.1 Color — "Ember"

Inspired by the PartyBox mood (black hardware, ring LEDs, heat-colored
light) without any JBL trade dress: no orange "!"-like marks, no JBL slab
logotype, original hues.

```css
:root {
  --bg:        #0B0B10;   /* near-black, slightly warm-blue */
  --surface:   #14141C;
  --surface-2: #1C1C26;
  --ink:       #F2F1F6;
  --muted:     #9A97AC;
  --faint:     #5A5870;

  --heat-1:    #FF3D2E;   /* ember red-orange  */
  --heat-2:    #FF8A00;   /* amber-orange      */
  --heat-3:    #FFC53D;   /* warm yellow       */
  /* signature gradient: linear/conic --heat-1 → --heat-2 → --heat-3 */

  --ok:        #35D0A0;   /* mint — not GitHub green */
  --warn:      #FFB300;
  --danger:    #FF4D5E;
}
/* per-scene washes (radial, low alpha, behind everything) */
.scene-off     { --wash: #FF2E3D; }   /* deep red   */
.scene-standby { --wash: #FFB300; }   /* amber      */
.scene-on      { --wash: #FF8A00; }   /* ember glow */
```

Glow technique: a fixed, blurred radial gradient at 8–14% alpha behind the
hero, plus a conic-gradient ring (`--heat-1 → --heat-3`) masked to a circle.
Cheap (pure CSS), atmospheric, no images.

### 7.2 Typography

- **Display** (device name, scene messages, "Playing"): a geometric
  grotesque with techno character — *Space Grotesk*, self-hosted as a single
  woff2 (~28 KB, weights via `font-variation`). No CDN: the appliance must
  render on a LAN with no internet.
- **UI/body**: `system-ui` stack (fast, native, free).
- **Numbers** (battery %, signal): `font-variant-numeric: tabular-nums`.
- Scale: 12 / 14 / 16 / 20 / 28 / 44 px; display at 600, body 400/500;
  letter-spacing +0.02em on small uppercase labels only.

### 7.3 Spacing

4px base grid: 8 / 12 / 16 / 24 / 32 / 48. Card padding 24 (16 on <400px).
State scenes center content in the viewport (flex, `min-height: 100dvh`,
content max-width 26rem). ON scene max-width 30rem single column — it reflows
to desktop by breathing, not by adding columns; there aren't enough elements
to justify a grid, and that's a feature. Touch targets ≥ 44px.

---

## 8. Motion

All motion is state, not decoration:

| Animation | Spec |
|---|---|
| Ring breathing (idle/standby) | opacity 0.55→0.8, 4s ease-in-out loop |
| Ring glowing (playing) | slightly larger amplitude + slow 20s hue rotate within heat range |
| Scene transitions | 300ms crossfade + 1.02→1.00 scale on the incoming scene |
| Value updates | 150ms fade through — never text snapping |
| Skeletons | shimmer on surface-2, only during first load |
| Buttons | 120ms ease color/transform, pressed scale 0.98 |

`@media (prefers-reduced-motion: reduce)`: kill loops, keep instant
crossfades. No spinners; pending states use the breathing treatment. No
layout is ever moved by an animation (no jumps).

---

## 9. Iconography & favicon

**Icons: Lucide**, inlined as SVG symbols in the HTML (~10 needed: power,
settings, battery, plug/zap, bluetooth, wifi, music, check, alert-triangle,
chevron, download). No icon font, no CDN, no emoji anywhere — including the
`🔒` in the network list (→ `lock`) and the battery strings.

**Favicon concept — "the lit ring":** a near-black rounded square; an
off-center circular ring stroked with the heat conic gradient (`#FF3D2E →
#FFC53D`), brightest at the top-right and fading to ember at the bottom-left,
like a speaker's LED ring caught mid-pulse; a subtle darker inner disc
suggesting a driver cone. Deliberately avoids: exclamation-mark geometry,
slanted-box containers, any letterform — the shapes JBL's mark owns.
Deliverables: 32px SVG (crisp at favicon size) + 180px apple-touch-icon +
512px AI-rendered variant with soft bloom for the touch icon if desired.
The same ring is the hero graphic in every scene — favicon and UI share one
identity.

---

## 10. Information removed outright

| Today | Verdict |
|---|---|
| BLE + A2DP MAC addresses on dashboard | → health sheet (expanded rows) / debug bundle |
| Firmware string as Speaker detail | → health sheet |
| "System · Running · vX" row | delete — a loaded page proves it; version → settings footer + health sheet |
| AirPlay "coming soon" row | delete until the feature exists |
| "Diagnostics" toast button | replaced by the real health strip |
| "About" toast | delete (version lives in settings footer) |
| Battery `state_of_health` / `cycle_count` | debug bundle only |
| Emoji (logo, battery, lock) | replaced by Lucide SVG |
| Header health pill | delete — the scene itself is the status |

## 11. Visibility by state

| Element | SETUP | PAIR | OFF | STANDBY | ON |
|---|---|---|---|---|---|
| WiFi picker | ✓ | – | – | – | – |
| Pairing flow | – | ✓ | – | – | re-pair via health sheet only |
| "Plug in the speaker" message | – | – | ✓ | – | – |
| Turn-on action | – | – | – | ✓ | (power toggle in hero) |
| Hero / playback / battery | – | – | – | – | ✓ (battery only if reported) |
| Spotify card | – | – | – | – | ✓ (problem state inline) |
| Health strip/sheet | – | – | – | – | ✓ |
| Settings gear | – | – | – | – | ✓ |
| Debug bundle | – | – | – | – | inside health sheet |

OFF and STANDBY intentionally hide settings and diagnostics; if the user
needs them while the speaker is off (rare, support-driven), the health sheet
remains reachable via a low-contrast text link at the very bottom of those
scenes — one concession to serviceability, visually near-invisible.

## 12. Further simplification (backend-assisted)

1. **`speaker_state` in `/health` + WS event** (§2) — **shipped.**
   `StatusSnapshot.speaker_state` (`partyboxd/device/manager.py`) derives
   off/standby/on from `connected` + a new `has_battery` capability flag
   (checked once via `device.battery is not None`, not inferred from a
   client-side "have we ever seen a reading" heuristic); `SpeakerStateChangedEvent`
   fires over the WS on any transition. The frontend's `deriveScene()` now
   reads `health.speaker_state` directly — this closed a real bug where the
   Portal defaulted to the ON scene (showing a "Turn off" button on an
   asleep speaker) if it loaded before ever observing a battery value.
2. **Push, don't poll.** Add `audio_changed` / `spotify_changed` /
   `pairing_progress` WS events; drop the 2s/15s/20s polling loops to a
   single slow reconciliation poll (~30s safety net). Kills the staleness
   windows and the pairing poll machinery.
3. **Expose `Supervisor.health()`** as `/api/v1/health/details` to power the
   health sheet with real data instead of a synthesized view.
4. **Volume: do not ship a slider yet.** `POST /volume` currently updates
   in-memory state while the BLE opcode is unconfirmed (ADR-022) — a slider
   that doesn't move the speaker is exactly the "misleading information" this
   redesign bans. Add the hero volume ring only when `source == "ble"` is
   real.
5. **Settings save**: keep explicit Save, but move the Spotify-restart
   consequence into the sheet ("Saving restarts Spotify (~5s)") instead of a
   post-hoc toast surprise.
6. Factory reset keeps its confirm, restyled as a typed sheet action rather
   than `window.confirm`.

## 13. Implementation constraints

- Stay **zero-build, single `index.html`** (plus one woff2 + favicon SVGs in
  `static/`): it matches the rsync-to-site-packages deploy loop and needs no
  toolchain. The state-machine structure above is ~the same JS size as today.
- **No CDN resources of any kind** — the Portal must fully render on a
  client whose only network is the appliance's LAN/AP (provisioning mode!).
- Ship order: (1) visual system + scenes with current API (frontend-only,
  keeps battery-proxy inference), (2) `speaker_state` + WS events, (3) health
  details endpoint + sheet, (4) delete the polling loops.
