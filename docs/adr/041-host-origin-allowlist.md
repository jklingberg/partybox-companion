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
  `PATCH`, `DELETE`) *and unconditionally for every WebSocket handshake*
  (`GET /api/v1/events`), against the same allowlist, but only when present —
  absence is not itself suspicious (see below). WebSocket handshakes need
  their own branch here: the handshake scope has no `method` key, so it can
  never match `_MUTATING_METHODS`, and — unlike `fetch()` — a cross-origin
  `new WebSocket(...)` is not blocked by the browser's same-origin policy at
  all, so skipping the check there would leave the richest live event stream
  (device address, firmware, battery, Spotify device name) readable from any
  origin under the default no-key config.

The `scope["server"][0]` comparison is what makes this work with zero
configuration: uvicorn populates it from the actual socket, not from the Host
header a client sent, so it already equals the current DHCP lease, a router
reservation, or the provisioning AP's fixed `10.42.0.1` — no LAN IP needs to
be hardcoded or read from settings, and provisioning endpoints reachable over
the AP just keep working with no special-casing.

**Deployment assumption — no reverse proxy today; document before adding
one.** This only works because uvicorn currently terminates the client
connection directly (the appliance binds port 80 itself, per
`docs/adr/020-appliance-hardening.md`'s `CAP_NET_BIND_SERVICE` grant) — no
reverse proxy sits in front of it, and Unix domain sockets are already
rejected by `docs/adr/007-tcp-only.md`. If a future deployment puts nginx,
Traefik, or another reverse proxy in front of partyboxd/companion,
`scope["server"][0]` would become the *proxy-to-uvicorn* hop's local address
(e.g. `127.0.0.1` if proxied over loopback — which happens to already be in
`_ALLOWED_HOSTNAMES`, so that specific case still works by coincidence — or a
container-bridge address like `172.17.0.2` if not, which would reject *every*
request). This middleware deliberately does not fall back to
`X-Forwarded-Host`/`X-Forwarded-For`: those headers are client-suppliable
unless a proxy is configured to strip and re-set them, and trusting them
without that guarantee would reopen the exact DNS-rebinding/CSRF gap this
middleware exists to close. Adding a trusted reverse proxy in front of the
appliance therefore requires revisiting this middleware (e.g. a configured
list of trusted proxy hops whose `X-Forwarded-Host` is honored) rather than
assuming it continues to work unmodified.

**Duplicate Host/Origin headers are rejected, not silently resolved.** The
first implementation read headers via `dict(scope["headers"])`, which keeps
only the *last* occurrence of a repeated header — verified (against a live
uvicorn/h11 server) that neither rejects a request with two Host headers or
two Origin headers itself. A request with a forged Host/Origin first and a
legitimate one last therefore passed the check under that implementation.
Fixed by scanning `scope["headers"]` for all occurrences and rejecting
outright when Host appears zero or more-than-once times, or Origin appears
more than once — matching RFC 7230 §5.4's requirement that a server reject
any request with more than one Host header. A compliant browser never sends
duplicate Host/Origin headers itself (both are forbidden headers `fetch()`
can't set directly), so this isn't reachable through a pure browser-mediated
CSRF/rebinding attack; it matters for non-browser LAN clients and for
resilience against any future intermediary that might disagree with this
middleware about which occurrence is authoritative (the request-smuggling
failure class this class of bug belongs to).

**`_hostname()` normalizes a trailing dot** (`partybox.local.` is the same
DNS name as `partybox.local` — the trailing dot denotes the DNS root and is
stripped by resolvers before lookup) so a client or resolver that includes it
isn't rejected as a foreign host. A bracket-less IPv6 literal in a Host
header (e.g. `Host: ::1` with no port) is not specially handled and is
rejected: RFC 3986/7230 require IPv6 literals to be bracketed in the Host
header, no compliant client sends it unbracketed, and recovering it without
brackets would require guessing where the address ends and a port begins.

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

This ordering is an implicit invariant of `companion/__main__.py`'s source,
not something Starlette enforces structurally — a future edit that
reorders the two `add_middleware()` calls (directly, or indirectly via a
refactor) would silently invert it, and none of the *behavioral* tests in
`test_host_origin_provisioning_interaction.py` would catch that, since they
construct their own app and reconstruct the correct order by hand rather
than exercising `__main__.py` itself.
`test_middleware_ordering_invariant.py` closes that gap with a structural
check against `__main__.py`'s actual source (asserting `create_daemon_app(`
appears before `add_middleware(CaptivePortalMiddleware` in `_run()`), so a
future reorder fails loudly instead of only manifesting as an AP-mode
provisioning regression discovered on hardware.

**WebSocket rejection mechanics:** `HostOriginMiddleware._reject()` sends a
`websocket.close` event with `code=4403` without first sending
`websocket.accept`. Per the ASGI spec, a close sent before accept has no
close code on the wire — the connection is refused at the HTTP-upgrade level
instead (verified against a live uvicorn server: the client sees the
handshake fail with a plain **HTTP 403**, not a WebSocket close frame
carrying 4403). The `code` value is therefore only meaningful to ASGI-layer
unit tests asserting on the event dict, not to a real client; it isn't
misleading in code, but is worth knowing when debugging with a real WS
client. Calling `receive()` once before `close()` (to consume the pending
`websocket.connect` event) is not strictly required by Starlette's own
`WebSocket` class, which allows sending `close` directly while still in the
`CONNECTING` state — but doing so anyway is the more defensive, broadly
portable choice across ASGI server implementations, and was verified (via a
live uvicorn + `websockets`-client round trip) to reject immediately with no
hang in the forged-Origin, same-Origin, and no-Origin cases.

**Rejected alternative — a configurable LAN-IP allowlist setting.** The
appliance's LAN IP is DHCP-assigned and not knowable at config time; adding
a setting the user has to keep in sync (or a background job to detect
address changes) is unnecessary complexity next to a check that is already
current by construction.

### 2. Extend the existing API-key dependency to the companion routers

`partyboxd.api.auth.make_auth_dependency` already existed and already gated
partyboxd's own private routes (`/speaker`, `/battery`, `/power/*`,
`/bluetooth/reset`). This is extended — not replaced — to also gate:

- `POST /api/v1/factory-reset`, `GET /api/v1/debug/bundle`,
  `POST /api/v1/audio/pair`, `POST /api/v1/spotify/restart`, and
  `POST /api/v1/volume` (`companion/services/router.py`) — the same
  `auth_dependencies` list already used for `GET /api/v1/health/details`
  (ADR-037). The corresponding `GET` status endpoints (`/audio`, `/spotify`,
  `/volume`) stay unauthenticated — read-only, no sensitive data, and the
  Portal must read them with no key configured.
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
- `GET /api/v1/events` (the WebSocket event stream) rejects a forged Origin
  on the handshake itself, in addition to its existing `api_key` query-param
  check — closing a cross-origin read that doesn't need DNS rebinding at all,
  since WebSockets aren't subject to the browser's same-origin policy.
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
