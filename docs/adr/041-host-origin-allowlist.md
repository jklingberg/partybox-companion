# ADR-041 — Host/Origin Allowlist for CSRF and DNS-Rebinding Defense

**Status:** Accepted
**Date:** 2026-07-22

---

## Context

The Technical Founder Review (`docs/review/04-security-review.md`, branch
`docs/technical-founder-review`) flagged two related findings, bundled as a
single P0/v1.0 gate item in `docs/review/08-roadmap-v2.md` and tracked as
GitHub issue #75:

- **SEC-02** — With the default config (no API key, the documented and
  expected default), every state-changing endpoint in
  `companion/services/router.py`, `companion/webui/router.py`, and
  `companion/wifi/router.py` executes for *any* HTTP request that reaches the
  appliance, from any origin. `POST /api/v1/factory-reset` wipes pairing,
  config, and bond; `POST /api/v1/wifi/connect` accepts arbitrary SSID/
  password; `PUT /api/v1/config` rewrites Spotify/audio settings. None of
  these check same-origin, and none of them respected the API key even when
  one *was* configured — a gap in the auth check, not missing infrastructure.
- **SEC-04** — `GET /api/v1/debug/bundle` (config, BLE address, firmware,
  battery serial, ~500 lines of journal output) had no auth at all, while the
  less-sensitive `GET /api/v1/health/details` was already gated by ADR-037.

Two attack paths, neither requiring LAN credentials:

1. **CSRF** — a page the owner's browser visits while on the same WiFi runs
   `fetch('http://partybox.local/api/v1/factory-reset', {method:'POST'})`.
   Simple cross-origin POSTs need no preflight.
2. **DNS rebinding** — a malicious site rebinds its own hostname to the
   appliance's LAN IP, then reads GET responses cross-origin (no same-origin
   policy violation once the hostname "is" the attacker's own).

## Decision

### 1. `HostOriginMiddleware` (`partyboxd/api/security.py`)

A single ASGI middleware, added inside `partyboxd.api.app.create_app()` so it
protects both headless `partyboxd` (`/power/on`, `/power/off`,
`/bluetooth/reset` are CSRF-open too under the default no-key config) and
every router `companion` layers on top:

- **Host** is checked on every request (GET included — DNS rebinding is not
  a mutating-method-only attack). Allowed: a small static set (`localhost`,
  `127.0.0.1`, `::1`, `partybox`, `partybox.local`) plus whatever address this
  *specific* connection actually reached the server on
  (`scope["server"][0]`).
- **Origin** is additionally checked for mutating methods (`POST`, `PUT`,
  `PATCH`, `DELETE`) against the same allowlist, but only when present —
  absence is not itself suspicious (see below).

The `scope["server"][0]` comparison is what makes this work with zero
configuration: uvicorn populates it from the actual socket, not from the Host
header a client sent, so it already equals the current DHCP lease, a router
reservation, or the provisioning AP's fixed `10.42.0.1` — no LAN IP needs to
be hardcoded or read from settings, and provisioning endpoints reachable over
the AP just keep working with no special-casing.

**Why Origin absence is allowed:** the documented `curl` workflows in this
repo's own `CLAUDE.md` (power on/off, health checks) send no Origin header at
all, and neither does any other non-browser client (the hardware test suite,
`journalctl`-adjacent tooling, HA scripts). Only a *mismatched* Origin — proof
a browser is relaying a cross-origin request — is rejected. A forged/absent
Host is rejected unconditionally; Host is present on every real HTTP request
and its absence or mismatch is the DNS-rebinding signal itself.

**Interaction with `CaptivePortalMiddleware`:** during AP-mode provisioning,
iOS/Android send probe requests (`GET /generate_204` etc.) with Host set to
the *probed* domain (e.g. `connectivitycheck.gstatic.com`), which wildcard
DNS resolves to the AP IP — structurally identical to DNS rebinding, and
`HostOriginMiddleware` would reject it. This does not break captive-portal
detection: Starlette's `add_middleware()` makes the most-recently-added
middleware outermost (`self.user_middleware.insert(0, ...)`, then
`build_middleware_stack()` wraps in `reversed()` order), and
`companion/__main__.py` adds `CaptivePortalMiddleware` *after*
`create_daemon_app()` already added `HostOriginMiddleware` — so
`CaptivePortalMiddleware` runs first, intercepts and 302s the probe paths,
and never calls through to `HostOriginMiddleware` for them. Once the CNA
popup navigates to the Portal, every subsequent request's Host is the AP IP
itself, which the `scope["server"][0]` check already allows.

**Rejected alternative — a configurable LAN-IP allowlist setting.** The
appliance's LAN IP is DHCP-assigned and not knowable at config time; adding
a setting the user has to keep in sync (or a background job to detect
address changes) is unnecessary complexity next to a check that is already
current by construction.

### 2. Extend the existing API-key dependency to the companion routers

`partyboxd.api.auth.make_auth_dependency` already existed and already gated
partyboxd's own private routes (`/speaker`, `/battery`, `/power/*`,
`/bluetooth/reset`). This is extended — not replaced — to also gate:

- `POST /api/v1/factory-reset` and `GET /api/v1/debug/bundle`
  (`companion/services/router.py`) — same `auth_dependencies` list already
  used for `GET /api/v1/health/details` (ADR-037).
- `PUT /api/v1/config` (`companion/webui/router.py`) — `GET /api/v1/config`
  stays public; it holds no sensitive data and the Portal must read it with
  no key configured.
- `POST /api/v1/wifi/connect` (`companion/wifi/router.py`) — **only** once
  `ProvisioningService.status.state == ProvisioningState.CONNECTED`. In every
  other state (`unprovisioned`, `ap_active`, `connecting`) auth is bypassed
  unconditionally, API key or not: the provisioning flow runs before any key
  can be entered (ADR-021 §6 — "no API key authentication" during
  provisioning is an explicit, already-accepted part of that design), over
  the appliance's own open AP. Gating only kicks in once the appliance is
  already on a home network, where an unauthenticated call could otherwise
  redirect it onto an attacker-controlled WiFi network.

In every case, *auth* remains an optional parameter defaulting to `None`
(unauthenticated) — the default no-key config keeps every existing Portal
flow (factory reset, settings save, pairing, wifi connect) working exactly as
before. Only the cross-origin/DNS-rebinding paths and, when a key *is*
configured, direct API calls that skip it are closed.

### 3. `GET /api/v1/debug/bundle` requires the same auth as `/health/details`

Same `auth_dependencies` list, reusing ADR-037's mechanism. The bundle is
strictly more sensitive (config, BLE address, firmware, battery serial,
journal excerpt) than `/health/details` (task/exception names only), so it
should never have had a lower bar.

## Consequences

- Both headless `partyboxd` and the full `companion` appliance now reject
  any request whose Host doesn't identify the appliance itself, closing
  SEC-04's DNS-rebinding path for every GET endpoint including `/health`,
  `/speaker`, `/battery`, and `/debug/bundle`.
- Mutating endpoints across both layers (`/power/on`, `/power/off`,
  `/bluetooth/reset`, `/factory-reset`, `/wifi/connect`, `PUT /config`,
  `/volume`, `/spotify/restart`, `/audio/pair`) reject a forged Origin,
  closing SEC-02's CSRF path — independent of whether an API key is
  configured.
- Configuring an API key now actually protects the full companion surface,
  not just partyboxd's original private routes — closing the specific gap
  SEC-02 called out ("leaves the entire companion services router open even
  when a key is configured").
- No new user-facing configuration, no reboot/reflash required — this ships
  as a source-only change (`packages/partyboxd/src/partyboxd/api/`,
  `packages/companion/src/companion/{services,webui,wifi}/`) deployable via
  the existing rsync-and-restart workflow in `CLAUDE.md`.
- Not addressed here (explicitly out of scope, tracked separately if
  needed): scrubbing WiFi-PSK-adjacent lines from the debug bundle's journal
  excerpt (SEC-04's "consider" recommendation, not a listed acceptance
  criterion); per-endpoint CSRF tokens (the Origin/Host check is the chosen
  mechanism instead, per the review's own recommendation); auth on the
  read-only `GET /api/v1/wifi/status` and `GET /api/v1/wifi/networks`
  (neither is in the acceptance criteria, and both are needed unauthenticated
  during provisioning regardless).
