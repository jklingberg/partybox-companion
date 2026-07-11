# 08 — Proposed Roadmap: v1.0 → v2.0

This re-sequences work around the findings in this review. It does not replace
`docs/roadmap.md` (which is the team's authored plan through M19/v1.0); it
proposes what should gate v1.0 and what the post-1.0 arc should be, with every
item traced to a finding ID so the reasoning is auditable.

Severity legend from [README.md](README.md): P0 blocks release, P1 fix soon,
P2 schedulable, P3 polish.

---

## Gate v1.0 on these (do not tag `v1.0.0` until done)

These are the "turns into a reputation event after launch" items. All are
small relative to their blast radius.

| Item | Findings | Why it gates |
|---|---|---|
| **Kill default SSH password** — first-boot random/forced-change or key-only, and default SSH to *disabled* | SEC-01 | Shared `pi/raspberry` + password auth on a party-network appliance is a CVE waiting for a byline. ADR-020 already said "before v1.0." |
| **Close the web CSRF/rebinding surface** — Host/Origin allowlist middleware + same-origin/CSRF check on all mutating endpoints; move factory-reset/wifi/config behind auth when a key is set | SEC-02, SEC-04 | Any website the owner visits on the LAN can factory-reset the box or exfiltrate the debug bundle. ~1 day of work. |
| **Fix the two shipped UX state bugs** | UX-01 (battery-as-awake proxy), UX-02 ("Ready to play" during audio failure) | Both are wrong-behavior-on-screen, both trivial, UX-01 mislabels the primary control on mains-only models. |
| **Fresh-`Pair()` on real hardware** | TEST-08, FAULT-05 | The first action a brand-new user takes has never run. Already an M19 goal — keep it hard. |
| **Fix or remove the volume API** — pin PipeWire node to 100% on connect (fixes INC-2) and back `POST /volume` with it, or 501 the endpoints | ARCH-04, INC-2 | An API that silently does nothing will be built on and will generate bug reports; INC-2 is already a confirmed release-blocking audio-UX defect. |
| **Atomic config writes** | DEBT-03 (write half), RACE-01 | SD cards fail mid-write; the recovery-by-losing-settings path is avoidable in 5 lines. |
| **Correct the stale CLAUDE.md sudoers claim** | (RC13 punch list) | Doc says a grant exists that doesn't; already flagged. |

Everything else in `docs/roadmap.md`'s M19 (docs review, API freeze, changelog,
version bump) stands.

**On the API freeze specifically:** before freezing `/api/v1/*`, resolve
DEBT-07 (two env namespaces) and decide the volume-endpoint fate (ARCH-04) —
both are forever-decisions after freeze.

---

## v1.1 — "Maintainable install base" (first post-1.0 milestone)

The theme: make it possible to *ship a fix at all*, and validate the second
model. Without v1.1 the whole project is frozen at its launch state.

1. **Update mechanism (DEBT-01, PROD-03)** — the top priority, full stop.
   - Release a versioned venv tarball alongside the `.img.xz`.
   - Portal-triggered updater: download → verify SHA256 + GPG signature →
     atomic `/opt` swap → service restart (ADR-017 says this is "straightforward";
     make it real).
   - Daily version check against GitHub releases → Portal "update available"
     banner (opt-out). Doubles as the only privacy-respecting install metric.
2. **Second-model validation (PROD-01, TEST-07)** — get one non-520 PartyBox
   through pairing + streaming. Publish the result in the README matrix
   regardless of outcome. This is a community ask as much as an engineering
   task — see OPP-01.
3. **Publish `partybox` on PyPI + protocol.md as canonical reference (OPP-01)**
   — verify the name (ADR-003), tag it as the headline deliverable.
4. **Debug-bundle + health-details auth alignment (SEC-04)** — gate the bundle.

## v1.2 — "Robust audio + real volume"

Theme: make the audio path own its lifecycle and stop routing through `pi`.

1. **PipeWire as `companion` / system-wide (ARCH-03)** — `main-embedded`
   profile (ADR-028 already validated it) or `enable-linger companion`. Delete
   the `/run/user/1000` chmod, the `-M pi@` restart, the `user@1000` ordering.
   Unblocks removing the `pi` account (SEC-01 follow-through).
2. **Single BlueZ broker subprocess (ARCH-01, DEBT-04)** — replace
   per-call interpreter spawns; unify connect/check/disconnect through it;
   enables `PropertiesChanged` push detection (delete the 60 s A2DP poll).
   First: build the minimal repro to confirm the dbus-fast conflict is real.
3. **Real volume actuation end-to-end (ARCH-04 continued)** — PipeWire node
   volume as the actuator behind the unified model; `source: "pipewire"`.
4. **librespot event hook for playback state (DEBT-02)** — `--onevent`; delete
   stderr guessing; unlocks reliable "paused" everywhere.

## v1.3 — "The SDK grows up" (protocol/event work)

Theme: the demux rewrite that unblocks everything downstream.

1. **Device-owned notification demultiplexer (ARCH-02, RACE-02/08, DEBT-05)** —
   one reader, route by opcode to futures + events stream. This single change:
   - deletes the drain/cancel/probe choreography,
   - makes capabilities concurrency-safe,
   - ships `device.events()` (currently discards button/state pushes — TODO in
     `device/partybox.py`),
   - makes standby detection push-based (delete the 15 s probe loop),
   - is the prerequisite for BLE hardware volume (ADR-022 Phase 2).
2. **Lighting capability (OPP-02)** — the flagship hardware-unique feature and
   the project's best marketing. Requires protocol work (capture + document +
   test per the RE discipline) but it's the compounding-value target.
3. **Adapter/hostname singleton removal (ARCH-05)** — adapter selection +
   first-boot hostname uniqueness; unblocks USB-dongle deployments (which are
   the *recommended mitigation* for the BCM4345 UART issue) and 2-appliance
   homes (which a party product will produce).

## v2.0 — "More than one speaker, more than Spotify"

Theme: the features deferred as "not core WiFi speaker," now that the core is
solid and maintained.

1. **AirPlay (M10, already planned)** — shairport-sync on the SpotifyService
   pattern; forces the audio-arbitration and volume-authority Phase 2 work to
   actually resolve.
2. **Opportunistic BLE connection model (PROD-04)** — connect-to-send /
   disconnect-idle so the JBL app coexists. Turn "Companion breaks the JBL app"
   into "Companion shares the speaker." Depends on the demux (v1.3) making
   connection churn cheap.
3. **Home Assistant HACS component (OPP-03)** — thin REST wrapper + config
   flow. Distribution multiplier.
4. **Karaoke / EQ / input-source capabilities (OPP-02)** — the remaining
   hardware-unique protocol frontier.
5. **Re-provisioning without reflash (RACE-07 follow-on)** — a way back to AP
   mode when WiFi is lost / router password changes, without a manual reboot
   into an unprovisioned state.

---

## Cross-cutting, do continuously

- **Add a regression test with every race/bug fix** ([05-testing-gaps.md](05-testing-gaps.md)).
- **Extract Portal logic for testability (TEST-01)** and stand up a
  `tests/integration/` tier (TEST-02) — the two structural test gaps that let
  the current bugs through.
- **Keep the RE discipline** — no speculative opcodes, real captures as
  fixtures. It is the project's strongest cultural asset (Part A of
  [01-architecture-review.md](01-architecture-review.md)).
- **Update these review docs in place** as findings are resolved — status
  lines, not deletions.
