# 07 — Product Strategy, Risks, and Opportunities

This is a founder's-eye view, not an engineering doc. It challenges the product
framing where the code and ADRs have quietly locked in assumptions.

---

## What the product actually is

Stripped of the appliance polish, the durable asset is: **an independent,
documented, MIT-licensed implementation of the JBL/Harman PartyBox BLE control
protocol, plus a clean-room A2DP-source appliance pattern.** The Spotify/AirPlay
streaming is *table stakes* — librespot and shairport-sync do the work, and
anyone can wire a Pi as an A2DP source with them in an afternoon. The moat, such
as it is, is the reverse-engineered protocol (power, battery, and — eventually —
lighting/EQ/karaoke) that *nothing else open source has*.

**Strategic implication:** the SDK and protocol docs are the crown jewels, not
the Portal. ADR-001 subordinates the SDK to the appliance ("library exists to
support the appliance"). That was right for shipping v1.0, but the *defensible,
compounding* value is the SDK + protocol corpus. Treat them as a first-class
product post-1.0, not an implementation detail.

---

## Positioning risks

### PROD-01 — Single-model, single-Pi validation is the existential product risk
The README matrix has one row. The entire "capability-based, works on any model"
promise (ADR-006) is unproven on a second device. If the first PartyBox 310 or
110 owner who flashes the image finds the FDDF offset differs (ADR-027 admits it
might), the name filter misses, or an opcode diverges, the project's core claim
fails publicly on their first attempt. **Do not market "works with any PartyBox"
until ≥2 models are validated.** Market "works with the PartyBox 520; help us
add yours." Honesty here is cheap insurance; over-claiming is a one-star review
generator.

### PROD-02 — Legal exposure is managed but not zero
ADR-012 is genuinely good clean-room hygiene. Residual risks: the vendor names
in code (`excelpoint.com`, `HARMAN_FDDF_UUID`, `"JBL"` string matching) are
observations of the wire (defensible), but any growth in profile — a popular
release, a HN front page — invites a HARMAN cease-and-desist regardless of
merit. Have a response plan: the clean-room provenance, the interoperability-
directive basis (already cited), and a named maintainer who is not personally
judgment-proof-exposed. Consider a foundation/org umbrella before the project
gets big enough to be worth a lawyer's afternoon.

### PROD-03 — No update path caps the addressable install base at "people who reflash"
DEBT-01, restated as strategy: without OTA, every user is frozen at their flash
version, every fix reaches only new users, and the security findings in
[04-security-review.md](04-security-review.md) become permanent for the
installed base. This isn't just technical debt — it structurally prevents the
project from having a *maintained* install base, which is the difference between
"a cool GitHub repo" and "a thing people rely on." Fund it first post-1.0.

### PROD-04 — The BLE-exclusivity trade-off silently disables the manufacturer app
While Companion runs, the JBL app can't connect (single-client GATT). So the
features Companion *doesn't* provide — firmware updates, lighting, EQ, karaoke —
become unreachable, and the user experiences it as "the JBL app is broken,"
not "Companion is holding the connection." This is a support-load and
reputation risk disproportionate to the code. Either (a) surface it prominently
("using the JBL app? Stop Companion first — here's the button"), or (b)
prioritize the opportunistic-connection model (roadmap) sooner than "post-1.0
someday," because it's the difference between coexisting with and cannibalizing
the manufacturer experience.

---

## Business / growth opportunities

Ranked by leverage on the actual asset (the protocol), not by flashiness.

### OPP-01 — Ship the SDK and protocol docs as the headline deliverable
`partybox` on PyPI + `docs/reverse-engineering/protocol.md` as the canonical
open reference for the PartyBox protocol is the thing that attracts
*contributors* (who bring the second/third model — solving PROD-01 for free)
and *downstream projects* (Home Assistant integrations, ESPHome ports, other
appliances). Low effort (the SDK is already clean and publishable), high
compounding return. Verify the PyPI name (ADR-003 flags it may be taken).

### OPP-02 — Lighting/karaoke/EQ are the features nothing else can offer
Spotify/AirPlay are commodity. The *hardware-unique* capabilities (ADR-010's
scope) — RGB lighting control, karaoke mic mixing, EQ presets — are what make a
"PartyBox companion" rather than "a Pi with librespot." These are the demo-able,
shareable, screenshot-able features that drive adoption, and they're squarely in
the SDK's stated scope. Post-1.0, lighting is the single highest-marketing-
value protocol target. (It's correctly deferred for v1.0 focus — but it's the
growth engine, not a footnote.)

### OPP-03 — Home Assistant native integration as a distribution channel
ADR-008 defers MQTT and a native HA component ("HA works as an HTTP client").
True, but the HA community is the largest concentration of exactly this
project's user (privacy-minded, self-hosting, owns smart-home hardware). A
lightweight HACS custom component wrapping the REST API — not a rewrite, just a
discovery/config-flow shim — is a distribution and credibility multiplier far
out of proportion to its cost. The REST API was designed for this; harvest it.

### OPP-04 — "Bring your own speaker" appliance pattern is reusable
The A2DP-source + BLE-control + captive-provisioning + Portal skeleton is
speaker-agnostic below the protocol layer. Marantz, Sonos-adjacent, other BLE
speakers with undocumented protocols could reuse the whole appliance with a new
`partybox`-shaped SDK. Not a v1.0 concern, but the layering (ADR-002/003) makes
this a real option — worth keeping the `partybox`→everything-else boundary
clean specifically so a second SDK can slot in.

### OPP-05 — Sell nothing; the monetization is reputation and optionally hardware kits
This is and should stay MIT/free. The realistic "business" upside is (a)
maintainer reputation, (b) an optional pre-flashed-SD / pre-configured-Pi-kit
sold at cost-plus for non-technical buyers (the vision's primary user "does not
need to write code" but *does* have to flash an SD card — a real drop-off
point), and (c) sponsorship once the install base is real. Don't distort the
architecture chasing revenue; the kit is a fulfillment play, not a product pivot.

---

## The one strategic reframing I'd push hardest

The roadmap and ADRs optimize for **"ship a polished single-model appliance."**
The asset that actually appreciates is **"the open PartyBox protocol and the
community that extends it."** These aren't in conflict, but they imply different
priorities post-1.0: publish the SDK loudly (OPP-01), make contributing a second
model frictionless (PROD-01), and treat lighting (OPP-02) as the flagship
capability rather than a deferred nicety. The appliance is the *demo* that makes
the protocol matter; don't let it become the only thing that exists.
