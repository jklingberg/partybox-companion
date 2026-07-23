# Hardware Validation — ARCH-04/INC-2 volume actuator (2026-07-23)

Not a full release validation run: this closes the outstanding hardware-validation
gap left by [#78](https://github.com/jklingberg/partybox-companion/issues/78) /
[PR #80](https://github.com/jklingberg/partybox-companion/pull/80), whose test
plan shipped with "Hardware validation on the Pi ... not run in this
environment" unchecked. All prior verification of the PipeWire volume actuator
(`companion.services.pipewire_volume`) was unit-test-level, with `wpctl`
subprocess calls mocked.

## Environment

| Item | Value |
|---|---|
| Image | `0.1.0rc14` + `main`-merged PR #80 (all three commits) deployed via rsync |
| Host | Pi at `partybox` (mDNS `.local` not resolving this session — router DNS used) |
| Speaker | JBL PartyBox (BR/EDR `50:1B:6A:14:FD:1D`) |
| PipeWire session | `pi` user's, at `/run/user/1000` (per ADR-019) |

## Finding: actuator never actually worked in production

First deploy + restart produced, on the very first A2DP connect:

```
WARNING companion.services.pipewire_volume PipeWire volume: wpctl set-volume exited 2: Could not connect to PipeWire
```

Root cause: `companion.service` runs as the `companion` system account
(`--no-create-home`, `nologin` — see ADR-019), which has no logind session and
therefore no `/run/user/<uid>` of its own. The live PipeWire/WirePlumber
session is `pi`'s, at `/run/user/1000`. `wpctl` resolves `@DEFAULT_AUDIO_SINK@`
via `$XDG_RUNTIME_DIR`, which was **unset** for the companion process
(confirmed via `/proc/<pid>/environ`) — the unit file's existing comment
claiming `RuntimeDirectory=companion` sets `XDG_RUNTIME_DIR=/run/companion`
was incorrect: for a system service (as opposed to a systemd `--user`
session), `RuntimeDirectory=` only exports `$RUNTIME_DIRECTORY`, a different
variable. `companion` had no route to the real PipeWire socket at all.

This means the ARCH-04 actuator, despite passing 101+ unit tests and two
rounds of code review, silently did nothing end-to-end since it was merged —
exactly the class of gap hardware validation exists to catch.

**Fix:** added `Environment=XDG_RUNTIME_DIR=/run/user/1000` to
`system/systemd/companion.service` (same UID-1000 assumption already
documented for `PULSE_SERVER` in `companion.env`), and corrected the
`RuntimeDirectory=` comment. Applied live via a systemd drop-in
(`/etc/systemd/system/companion.service.d/override.conf`) for this session —
the repo fix will take effect on the appliance at the next image
rebuild/reflash per this project's deployment model (unit file changes aren't
rsync-deployable).

## Scenarios validated (post-fix)

All three confirmed against the real PipeWire sink via independent `wpctl`
readback (not just the companion API reporting success against itself).

### 1. `POST`/`GET /api/v1/volume` actually drive audible output (ARCH-04 core)

- `POST /api/v1/volume {"level": 42}` → `204`
- Independent `wpctl get-volume @DEFAULT_AUDIO_SINK@` → `Volume: 0.42` (not
  mocked, not the companion process — a fresh `systemd-run` invocation)
- `GET /api/v1/volume` → `{"level": 42, "source": "pipewire"}`

**PASS.** The endpoint changes real output volume; `GET` reads it back live.

### 2. Fresh connect with no known volume pins to 100% (INC-2)

- Manually set the sink to `0.30` via `wpctl` (simulating WirePlumber's quiet
  default) outside the companion process
- Restarted `companion.service` (clears in-memory `VolumeState` to `None`)
- On the resulting fresh A2DP connect (`A2DP connection established` in the
  journal), independent `wpctl get-volume` → `Volume: 1.00`

**PASS.** A connect with nothing recorded in `VolumeState` corrects a quiet
sink to 100%, closing INC-2 at the code layer as designed.

### 3. Reconnect with a known volume preserves it, does not reset to 100%

(This is the behaviour added in the afb0136 follow-up commit, itself a
response to code review on the first version of this actuator.)

- With volume at 42% (from scenario 1), powered the speaker off via
  `POST /api/v1/power/off`, confirmed `audio_ready: false`
- Powered back on via `POST /api/v1/power/on`, waited for `audio_ready: true`
- Independent `wpctl get-volume` → `Volume: 0.42` (unchanged)
- `GET /api/v1/volume` → `{"level": 42, "source": "pipewire"}`

**PASS.** A routine reconnect (the documented "speaker drops A2DP on idle"
behaviour) does not clobber a level the user already set — ADR-022's
last-write-wins model holds across a real power cycle, not just in mocked
unit tests.

## Outcome

All three acceptance-criteria scenarios from #78 are now confirmed on real
hardware, plus a previously-undetected deployment bug (missing
`XDG_RUNTIME_DIR`) that made the entire actuator inert in production is fixed
in `system/systemd/companion.service`. The appliance under test was updated
live via a systemd drop-in for this validation; the committed unit-file fix
needs a future image rebuild to reach appliances that don't get the drop-in
applied by hand.
