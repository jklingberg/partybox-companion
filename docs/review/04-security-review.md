# 04 — Security Review

Threat model: a network-connected appliance on a home LAN, physically
accessible, running a web server that controls hardware and can power off the
host. The realistic adversaries are (a) other devices/people on the same LAN
(house guests at a party — the literal use case), (b) any website the owner's
browser visits while on that LAN (CSRF / DNS-rebinding), and (c) anyone with
brief physical access. The project has thought about *Bluetooth* security
carefully (ADR-027 scoped bondable mode is genuinely good); it has thought
about *network/web* security much less.

---

### SEC-01 — Ships with well-known default SSH credentials `pi` / `raspberry`
**Severity:** P0 · **Status:** OPEN · **Where:** `image/install.sh` (`echo "pi:raspberry" | chpasswd`, `PasswordAuthentication yes` drop-in), ADR-020

Every appliance boots with the same password-authenticated sudo-capable SSH
account. On the target's own shared-WiFi use case (a party), this is a root
shell for anyone who reads the project README. ADR-020 explicitly flagged this
as "must be re-evaluated before v1.0" — v1.0 is now. The `PasswordAuthentication
yes` drop-in makes it worse than stock Bookworm (which defaults to
`prohibit-password`).

This is the finding most likely to become a CVE and a headline
("open-source PartyBox project ships default password"). The mitigations are
all standard:

**Recommendation (pick one, in order of preference):**
1. **First-boot forced credential setup** — generate a random password on
   first boot, print it to the serial console + show it in the Portal's first
   screen, require change on first SSH login. Or:
2. **Key-only, provisioned via Portal** — no password auth; the first-boot
   Portal flow accepts an SSH public key (optional; SSH is a debug convenience,
   not required for the product). Or, at absolute minimum:
3. **Per-image random password** baked at build with the value in the release
   notes — weak, but not *shared across every device on earth*.

Also: SSH is a debug tool for an appliance whose entire value proposition is
"no terminal." Consider shipping it **disabled by default**, enabled via a
Portal toggle. The product does not need SSH; the developer does.

### SEC-02 — State-changing endpoints are unauthenticated and CSRF-open
**Severity:** P0 · **Status:** OPEN · **Where:** `companion/services/router.py`, `webui/router.py`, `wifi/router.py`

With the default config (no API key — the documented and expected default),
these all execute for **any** HTTP request that reaches the appliance:

- `POST /api/v1/factory-reset` — wipes pairing, config, bond.
- `POST /api/v1/power/{on,off}` — actually 503s without a key only if a key is
  set; default is open.
- `POST /api/v1/volume`, `POST /api/v1/spotify/restart`, `POST /api/v1/audio/pair`.
- `PUT /api/v1/config`.
- `POST /api/v1/wifi/connect` — submit arbitrary SSID/password.

Two attack paths, neither requiring LAN credentials:

1. **CSRF.** These are simple requests (`POST` with no custom headers required;
   `PUT` with `Content-Type: application/json` — reachable via `fetch` with
   `mode: no-cors` for the side effect, or a form for the POSTs). Any web page
   the owner opens while on the same WiFi can `fetch('http://partybox.local/api/v1/factory-reset', {method:'POST'})`
   and factory-reset the appliance, or harvest a debug bundle
   (`GET /api/v1/debug/bundle` — includes config + journal logs) via a
   navigation. No same-origin needed for fire-and-forget POSTs.
2. **DNS rebinding.** `partybox.local` / the appliance IP has no `Host`-header
   validation; a malicious site can rebind its own hostname to the appliance's
   IP and then read responses cross-origin. The debug bundle and
   `GET /api/v1/health/details` (exception strings, possibly containing paths /
   BLE addresses) then leak to the attacker.

The API-key mechanism exists but is **opt-in and off by default**, protects
only the partyboxd private routes (`/speaker`, `/battery`, `/power/*`,
`/health/details`), and leaves the entire companion services router
(factory-reset, config, wifi, volume, pair, spotify) open *even when a key is
set*. The router docstring rationalizes this ("read-only appliance state, no
sensitive data") — but factory-reset, wifi/connect, and config PUT are neither
read-only nor non-sensitive.

**Recommendation:**
1. **Add a `Host`/`Origin` allowlist middleware** (accept only `partybox.local`,
   `partybox`, `localhost`, and configured LAN IPs) — this alone kills DNS
   rebinding and most CSRF, costs ~15 lines, and needs no user-facing key.
2. **Require a same-origin check or CSRF token for all mutating endpoints.**
   The Portal is same-origin; enforce it.
3. **Move factory-reset, wifi/connect, and config PUT behind auth** when a key
   is configured (currently they bypass it). Reconsider the "services router is
   unauthenticated" blanket.
4. Keep provisioning endpoints open **only while in AP mode** (they're the one
   legitimately-unauthenticated case, and only then).

### SEC-03 — Idle-battery shutdown is a remotely-triggerable DoS via spoofed advertisement
**Severity:** P2 · **Status:** OPEN · **Where:** `companion/__main__.py::_idle_battery_shutdown`, ADR-038

The watcher powers off the Pi after 30 min "standby on battery" or 90 s "off on
last-known-battery." `last_known_on_battery` freezes at the last reading and is
trusted through disconnects (ADR-038 accepts this). An attacker who can make
the speaker appear off/asleep — trivially, by holding a BLE connection to the
speaker (single-client GATT limit, documented in open-questions) so the daemon
can't probe it, i.e. just pairing their phone to it — combined with a prior
battery reading, drives the appliance to power itself off. Recovery requires
physical access (10 s button hold). At a party, "guest's phone connects to the
speaker → Companion shuts down" is a plausible *accidental* trigger, not just a
malicious one.

**Recommendation:** require a *confirmed live* battery/mains reading within the
idle window rather than trusting an arbitrarily-stale one across a disconnect
the appliance can't explain; or gate the whole feature on the speaker having
been confirmed on-battery *recently* (bounded staleness — the exact thing
ADR-038 declined to add). At minimum, log loudly and consider a longer off-state
grace than 90 s.

### SEC-04 — `GET /api/v1/debug/bundle` is unauthenticated and includes journal logs
**Severity:** P2 · **Status:** OPEN · **Where:** `companion/services/router.py::get_debug_bundle`

The bundle contains `config.json` (Spotify name, speaker MAC), full device
snapshot (BLE address, firmware, battery serial/health), system platform/node,
and **500 lines of journal output**. Journal lines can contain WiFi SSIDs,
error messages with paths, BLE addresses, and — depending on what
subprocesses log — potentially more. It is a `GET` with no auth, so both CSRF
navigation and DNS-rebinding (SEC-02) exfiltrate it. `/health/details`
(exception strings) is at least auth-gated per ADR-037; the debug bundle,
which is strictly more sensitive, is not. Inconsistent and wrong-way-round.

**Recommendation:** gate the debug bundle behind the same auth as
`/health/details`; scrub or omit WiFi PSK-adjacent lines; note in the ADR-037
lineage that the bundle is the bigger leak.

### SEC-05 — Open provisioning AP + plaintext credential submission has a real (accepted) exposure window
**Severity:** P2 · **Status:** ACCEPTED-RISK (revisit) · **Where:** ADR-021, `wifi/router.py`

ADR-021's reasoning (iOS CNA needs HTTP:80, no TLS) is correct and the
threat-model argument (phone 3 m from Pi) is *mostly* fine. But the AP is
**open** (no WPA), so the home WiFi PSK the user types travels over
unencrypted 802.11 during provisioning — anyone within radio range (a
neighbour, a parked car) can capture it passively. This is the same trade
balena/RaspAP make, so it's defensible, but it is a real exposure the ADR
frames as fully solved. Note it honestly in user docs.

**Optional hardening:** WPA2 AP with the passphrase derived from/printed as the
last MAC octets or shown on the CNA landing before credential entry — adds
friction, closes passive capture. Judgment call; document the decision either way.

### SEC-06 — polkit rule grants `companion` NetworkManager access via a broad predicate
**Severity:** P3 · **Status:** ACCEPTED-RISK · **Where:** `image/install.sh` (51-companion-nm.rules), ADR-021

ADR-021 consciously widened this from three action IDs to the NM namespace
because the narrow list was fragile across NM versions. Defensible for a
no-login system account, and documented. Flagged only so a future reviewer
doesn't mistake the breadth for an accident. The logind rule (52-…) is
correctly narrow (two stable action IDs) — good contrast.

### SEC-07 — No rate limiting on any endpoint, including pairing and wifi-connect
**Severity:** P3 · **Status:** OPEN

`POST /api/v1/audio/pair` starts a 60 s radio-intensive scan; `wifi/connect`
drives NM; both are unauthenticated. No throttle. A script can pin the
Bluetooth radio (starving BLE control + A2DP) or thrash NM indefinitely.
Single-user appliance makes this low-severity, but a `409`-on-in-progress
(already present for pairing) plus a simple per-endpoint cooldown closes it.

### SEC-08 — Portal serves user-controlled strings; audit the two innerHTML paths
**Severity:** P3 · **Status:** OPEN (verify) · **Where:** `webui/static/index.html`

Most dynamic text uses `textContent` or the `esc()` helper (good — SSIDs go
through `esc`). But `renderHealthSheet`/`healthItems` build rows via
`innerHTML` with `i.text` interpolated unescaped, and `i.text` for the
Companion row includes `humanizeTaskName(down.name)` — task names are
developer-controlled today, so not exploitable now, but the pattern (unescaped
innerHTML of a backend-sourced string) is one refactor away from XSS if a task
name ever becomes user-influenced. Route all backend strings through `esc()`
in innerHTML contexts on principle.

---

## Security posture summary

| Layer | Assessment |
|---|---|
| Bluetooth pairing/bonding | **Strong** — scoped bondable mode, Just-Works risk understood (ADR-027) |
| systemd hardening | **Strong** — dedicated user, NoNewPrivileges, ProtectSystem=strict, minimal caps |
| Privilege escalation design | **Good** — polkit/D-Bus over sudo, rejected piecemeal grants (ADR-028/038) |
| **Network/web auth** | **Weak** — CSRF-open mutating endpoints, no Host validation, opt-in key with gaps (SEC-02, SEC-04) |
| **Default credentials** | **Unacceptable for v1.0** — shared `pi/raspberry` + password SSH (SEC-01) |
| **Update/patch path** | **Absent** — see DEBT-01; no way to ship any of these fixes to installed devices |
| Cross-user isolation | **Broken by design** — `/run/user/1000` opened to 755 (ARCH-03) |

SEC-01, SEC-02, and DEBT-01 together are the release-gating security story:
a shared root password, a web surface any website can drive, and no way to
push a fix afterward. Fix SEC-01 and SEC-02 before v1.0; commit to DEBT-01 as
the first post-1.0 milestone.
