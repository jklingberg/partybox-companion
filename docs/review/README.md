# Technical Founder Review — 2026-07-11

A full-codebase review of PartyBox Companion performed at the M18→M19 boundary
(pre-v1.0), from the perspective of an incoming technical owner. Every line of
source, all 38 ADRs, the roadmap, the validation suite, the image pipeline, and
the Portal were read for this review.

## How to maintain these documents

These files are **living registers**, designed to be updated by any contributor
or coding model, including ones with less context than the original reviewer:

- Every finding has a **stable ID** (`ARCH-03`, `SEC-01`, …). Never renumber or
  reuse IDs. New findings append at the end of their section.
- Every finding has a **Status** line: `OPEN`, `ACCEPTED-RISK`, `IN-PROGRESS`,
  `FIXED (<commit/PR>)`, or `WONTFIX (<reason>)`. When you fix something,
  update the status *in place* — do not delete the finding.
- Every finding cites file paths and, where useful, the governing ADR. If a
  fix changes the cited code, update the citation.
- Severity scale: **P0** (release blocker), **P1** (fix soon after release),
  **P2** (real but schedulable), **P3** (note / polish).

## Documents

| File | Contents |
|---|---|
| [01-architecture-review.md](01-architecture-review.md) | What the architecture gets right, what it gets wrong, and challenged assumptions |
| [02-technical-debt.md](02-technical-debt.md) | Hidden debt register with severity and remediation |
| [03-concurrency-and-races.md](03-concurrency-and-races.md) | Race conditions and async hazards, verified against source |
| [04-security-review.md](04-security-review.md) | Threat model and concrete vulnerabilities |
| [05-testing-gaps.md](05-testing-gaps.md) | What the 442-test suite does not cover |
| [06-ux-review.md](06-ux-review.md) | Portal and onboarding UX findings, including two real bugs |
| [07-product-strategy.md](07-product-strategy.md) | Positioning, risks, growth, business opportunities |
| [08-roadmap-v2.md](08-roadmap-v2.md) | Proposed v1.0 → v2.0 sequencing |
| [09-hardware-and-resource-efficiency.md](09-hardware-and-resource-efficiency.md) | Hardware-safety and CPU/RAM/battery/Bluetooth-traffic audit (added 2026-07-22) |

## One-paragraph verdict

This is an unusually disciplined codebase for a pre-1.0 hobby-scale appliance:
the layering is real and enforced, decisions are recorded with their evidence,
hardware behaviour is validated rather than assumed, and the test suite covers
the protocol and service layers well. The dominant risks are **not** in the
Python: they are (1) shipping with default `pi/raspberry` SSH credentials,
(2) unauthenticated state-changing HTTP endpoints reachable via CSRF/DNS
rebinding from any web page a user on the LAN visits, (3) no update mechanism
of any kind, (4) a product validated on exactly one speaker model and one Pi
model, and (5) an audio path that routes through another user's login session.
None of these are hard to fix; all of them are the kind of thing that turns
into a reputation event after launch instead of a code review comment before
it.

## Addenda

- **2026-07-22 — Hardware safety & resource efficiency pass**
  ([09-hardware-and-resource-efficiency.md](09-hardware-and-resource-efficiency.md)),
  done after 14 more commits had landed on `main` (audio-focus detection, the
  WirePlumber volume fix, real Spotify playback state, manual Bluetooth
  reset). Verdict: nothing found can damage the PartyBox hardware — the
  write surface is three ATT-flow-controlled opcodes with no firmware-update
  path, and power cycling self-limits to ADR-034's own ~40 s floor. The real
  findings are resource waste (subprocess-per-D-Bus-call CPU churn, a fixed
  15 s dual-probe liveness cadence, A2DP paging a beacon-less "off" speaker
  forever) that is invisible on the Pi 5/mains-powered dev setup and decisive
  on the stated Pi Zero 2 W / battery-adjacent target. Two findings restate
  [ARCH-01](01-architecture-review.md#arch-01)/[ARCH-02](01-architecture-review.md#arch-02)
  with quantified cost rather than opening new ones; the rest are new
  `PERF-*` IDs.
