# ADR-021 — Network Provisioning Architecture

**Status:** Accepted
**Date:** 2026-06-28
**Milestone:** M14

---

## Context

M14 removes the last piece of Raspberry Pi knowledge from the onboarding experience. A user who has just flashed a Companion image should be able to provision WiFi from their phone — no terminal, no SD card editing, no SSH.

The provisioning flow must work reliably on the two dominant mobile OSes (iOS and Android) without any pre-installed app. The only tool a user has is the device's built-in browser.

Before implementing, the proposed architecture was validated against known behaviour of modern operating systems, mature open-source projects (balena wifi-connect, RaspAP), and NetworkManager documentation. This ADR records what was confirmed, what was corrected, and what was added.

---

## Decisions

### 1. NetworkManager for AP mode (not hostapd)

**Decision:** Use NetworkManager's native AP support (`802-11-wireless.mode ap` with `ipv4.method shared`) to create the temporary access point. Do not install or invoke hostapd separately.

**Rationale:** Pi OS Bookworm ships NetworkManager 1.44+, which has fully capable built-in AP mode. When a connection profile is created with `ipv4.method shared`, NM:

- Creates the virtual AP interface
- Assigns the host a fixed IP on the AP subnet (default 10.42.0.1/24)
- Starts its own internal dnsmasq instance for DHCP
- Sets up IP masquerading for internet sharing (unwanted here, but harmless during provisioning)

The companion service interacts with NM via `nmcli` subprocess calls. No additional AP software is required. This matches what balena wifi-connect does when NM is present — it was confirmed that wifi-connect only invokes hostapd as a fallback when NM is absent.

**Rejected alternatives:**
- **hostapd + dhcpd directly** — works but requires manual coordination of AP, DHCP, and DNS state. NM handles this coordination reliably and is already installed.
- **iwd** — not installed on Pi OS by default; would replace NM entirely.
- **balena wifi-connect** — a Go binary that wraps the same NM primitives plus a CORS proxy; adds a binary dependency we don't need. Documented community issues with AP interface sticking in `UNAVAILABLE` state and DHCP lease renewal failures on Pi 3 B+.

---

### 2. NM's internal dnsmasq with wildcard DNS (not a separate dnsmasq service)

**Decision:** Extend NM's internal dnsmasq with a wildcard DNS entry via `/etc/NetworkManager/dnsmasq-shared.d/captive.conf`. Do not run a separate dnsmasq service.

**Rationale:** When NM starts the AP with `ipv4.method shared`, it launches an internal dnsmasq instance bound to the AP interface for DHCP. This instance reads drop-in configuration from `/etc/NetworkManager/dnsmasq-shared.d/`. A single config file:

```ini
# /etc/NetworkManager/dnsmasq-shared.d/captive.conf
address=/#/10.42.0.1
```

causes all DNS queries from AP clients to resolve to the appliance IP. This is the correct mechanism: the Pi becomes the authoritative DNS resolver for every connected client during provisioning, making every hostname resolve to the Portal.

Running a second, separate dnsmasq instance would conflict with NM's own instance on the same interface.

**Wildcard DNS is necessary (not optional).** Both iOS and Android detect captive portals by probing specific URLs (`captive.apple.com`, `connectivitycheck.gstatic.com`, etc.). Without DNS interception, these probes reach the real servers and the OS concludes the network is unconstrained — no popup appears and the user must open a browser manually. Wildcard DNS is the mechanism that triggers automatic captive portal detection.

---

### 3. Open AP (no password) for the provisioning network

**Decision:** The `PartyBox Companion Setup` AP is open (no WPA password). Users connect without entering a passphrase.

**Rationale:** The provisioning AP is temporary. Its security model relies on the captive portal itself, not on WPA. Requiring a password to connect to the setup network adds friction with no security benefit — the user would need to learn and type a random password just to reach the screen where they enter their actual WiFi password.

Consumer devices from the same category (Google Nest, Apple TV, Philips Hue Bridge setup) use open or QR-code-scan provisioning APs for this reason.

The captive portal interaction (selecting SSID, entering home network password) is conducted over the local AP link where no external attacker can observe it. Home WiFi credentials are never transmitted to the internet — they go from the browser to the Pi's local HTTP API on the AP subnet.

**Rejected alternatives:**
- **WPA2-PSK with a fixed passphrase printed on the device** — adds friction; the passphrase must be communicated to the user (documentation, label).
- **WPA2-PSK with a random passphrase shown on an OLED display** — we have no display.

---

### 4. HTTP on port 80 is required in M14, not deferred to M15

**Decision:** The Companion service must bind and serve on port 80 during provisioning mode. This is a prerequisite for M14, not a consequence of M15.

**Rationale:** This was the most significant finding from the architecture validation.

iOS's Captive Network Assistant (CNA) — the system popup that opens when connecting to a captive portal — renders pages exclusively over HTTP. It does not follow HTTPS redirects and does not present TLS certificate dialogs. If the Portal is served on any port other than 80, the iOS CNA shows a blank or error page. The auto-popup experience fails completely.

Android's captive portal browser is more permissive (it will follow to port 8080 once the popup opens), but the *initial probe* that triggers the popup must receive a response on port 80.

M15's goal — "Portal reachable at `http://partybox.local` on port 80" — was originally stated as a post-provisioning polish step. This goal turns out to be a hard prerequisite for M14 to work on iOS. The implementation must bind port 80 from the start of M14. M15 becomes validation of the end-to-end experience (hostname, mDNS, post-provisioning access) rather than a new capability.

The `companion` user has `AmbientCapabilities=CAP_NET_BIND_SERVICE` in the systemd unit, so port 80 binding without root is already wired up.

---

### 5. Captive portal probe interception with HTTP 302 (not 204)

**Decision:** The FastAPI application intercepts well-known captive portal probe paths and returns HTTP 302 redirects to the Portal root. It must not return 204.

**Rationale:** This detail was absent from the original M14 proposal and is critical for the auto-popup to work.

iOS and Android probe specific URLs to determine network status:

| OS | Probe path |
|---|---|
| iOS / macOS | `GET /generate_204` on `captive.apple.com` |
| Android | `GET /generate_204` on `connectivitycheck.gstatic.com` |
| Windows | `GET /connecttest.txt` on `www.msftconnecttest.com` |

Because wildcard DNS resolves all these hostnames to the appliance IP, these requests arrive at the Companion Portal HTTP server. The response code determines what happens:

- **HTTP 204 (No Content)** — tells the OS "internet is fully available, no captive portal." The OS dismisses the probe silently. No popup appears. This is the wrong response.
- **HTTP 302 redirect to Portal** — tells the OS "you've been intercepted, here's the login page." The OS opens the CNA popup. This is what we want.

During provisioning mode, the Portal adds a middleware layer that catches these probe paths and returns 302 to the Portal root (`http://<ap-ip>/`). In normal operation mode, this middleware is inactive and the paths return 204 (correctly indicating full connectivity).

Known probe paths to intercept:

```
/generate_204
/hotspot-detect.html
/library/test/success.html
/connecttest.txt
/ncsi.txt
/redirect
/success.txt
```

---

### 6. Provisioning is an explicit Portal mode, not inferred component state

**Decision:** The Portal tracks an explicit operating mode (`Mode.PROVISIONING` or `Mode.NORMAL`) via a `setMode()` function. The provisioning screen and the dashboard are never shown simultaneously. `setMode()` is the single point of truth for which main screen is active.

**Rationale:** Without an explicit mode, the Portal would infer its layout from individual component states (e.g. "hide the speaker row when WiFi is not provisioned"). As the Portal grows, this inference becomes fragile — adding a new component requires auditing all mode-sensitive display logic instead of updating one call to `setMode()`.

The two modes are architecturally distinct: in provisioning mode there is no home network, no Spotify service, no speaker connection, and no API key authentication. The normal dashboard is meaningless until provisioning is complete. Making this distinction explicit in the code reflects reality.

---

### 7. HTTP-only during provisioning — HTTPS is intentionally not used

**Decision:** The provisioning Portal is served over plain HTTP on port 80. HTTPS is explicitly rejected for the provisioning flow.

**Rationale:** This is the most counterintuitive decision in the provisioning architecture and is documented here to prevent future "improvements" that would silently break the captive portal experience.

**iOS Captive Network Assistant (CNA)** — the system popup that appears when a phone connects to a captive portal — is not a browser. It is a sandboxed WebView with deliberately restricted behaviour:

- It does not present TLS certificate confirmation dialogs.
- It does not follow redirects to HTTPS.
- It renders pages only over HTTP on port 80.
- If the Portal redirects to `https://`, the CNA shows a blank page and the user must open Safari manually.

**Android's captive portal browser** is more permissive about ports once the popup is open, but the *initial probe* that triggers the popup (`connectivitycheck.gstatic.com/generate_204`) must receive a non-204 response on port 80. If port 80 is closed, the popup does not appear.

**The security model is sound despite the absence of TLS.** Home WiFi credentials travel from the phone's browser to the Pi's loopback-adjacent AP subnet — not over the internet, not over the home network, and not over a path accessible to any external party. The threat model for this link (a phone 3 metres from a Pi) does not require encryption. Once provisioned and on the home network, the Portal is accessible via `http://partybox.local`, where TLS with a self-signed certificate would cause browser warnings that are more harmful to UX than the absence of TLS itself.

**Rejected alternatives:**
- **HTTPS with a self-signed certificate** — certificate warnings break the CNA popup and require user override in all browsers. The provisioning flow breaks on iOS entirely.
- **HTTPS with a real certificate (e.g. Let's Encrypt)** — requires DNS ownership and internet access, neither of which exist during provisioning.
- **HTTPS with a pinned certificate bundled at build time** — the CNA does not do certificate pinning and will not present a trust dialog. The result is a blank CNA page on iOS.

---

### 8. nmcli subprocess for NM interaction (not python-dbus)

**Decision:** The provisioning state machine calls `nmcli` via asyncio subprocesses. Direct D-Bus via `python-dbus` or `dbus-python` is not used.

**Rationale:** `nmcli` is installed on every NM system, requires no additional Python dependencies, and its output is stable and documented. The provisioning path is not latency-sensitive; polling NM state every few seconds via `nmcli connection show` is acceptable.

D-Bus provides async event notifications without polling but adds a compiled C extension (`dbus-python`) as a runtime dependency. The `python-networkmanager` package wraps this but has version compatibility issues between NM releases.

The Companion codebase already uses subprocess management for librespot (`SpotifyService`). Using the same pattern for NM keeps the implementation consistent.

---

### 9. ProvisioningService in companion/services/

**Decision:** WiFi provisioning is implemented as a `ProvisioningService` class in `companion/services/`, following the same pattern as `SpotifyService`. The service manages AP lifecycle, state transitions, and NM interaction. The `/api/v1/wifi/*` router queries and commands this service.

**Rationale:** The existing service pattern (`start()`, `stop()`, status polling, graceful shutdown) is a natural fit for the AP lifecycle. The provisioning service starts on Companion startup, checks whether provisioning is needed, and either enters AP mode or exits immediately. This keeps the provisioning logic encapsulated and testable separately from the main application.

---

### 10. Hold AP until NM STA connection reaches ACTIVATED

**Decision:** After the user submits WiFi credentials via `POST /api/v1/wifi/connect`, the appliance holds the AP active until NetworkManager confirms the new STA connection has reached the `ACTIVATED` state. Only then is the AP torn down.

**Rationale:** This prevents the most common failure mode: the AP disappears while the Pi is still connecting to the home network. If the credentials are wrong, or the network is temporarily unreachable, the user loses the provisioning interface before they can try again.

The `ProvisioningService` polls `nmcli connection show <ssid>` at short intervals after credential submission. A timeout (e.g., 30 seconds) transitions back to AP mode with an error state if ACTIVATED is not reached, allowing the user to retry.

---

## State machine

The happy path and all failure paths are modelled explicitly. A reboot is
never required — every failure returns to AP_ACTIVE so the user can retry.

```
BOOT
  │
  ├─ NM has active WiFi STA (ACTIVATED)?
  │      Yes ──► NORMAL OPERATION
  │      No  ──► PROVISIONING
  │
PROVISIONING
  │
  ├─ Create NM AP connection (companion-ap, open, ipv4.method shared)
  ├─ Bind Portal on port 80
  ├─ Enable captive probe interception middleware (302 redirect)
  │
AP_ACTIVE   [reason=null, message=null]
  │  (user connects phone to "PartyBox Companion Setup")
  │  (OS auto-detects captive portal via wildcard DNS → opens browser popup)
  │  (GET /api/v1/wifi/networks → user selects SSID)
  │  (POST /api/v1/wifi/connect with SSID + optional password)
  │
CONNECTING
  │  (AP torn down; nmcli device wifi connect <ssid> running)
  │  (Portal polls GET /api/v1/wifi/status every 2 s)
  │
  ├─ nmcli exit 0 ──────────────────────────────────────────► CONNECTED
  │                                                               │
  ├─ Auth failure (wrong password / 802.1X supplicant error)     └─► NORMAL OPERATION
  │      └─► AP_ACTIVE [reason=authentication_failed]
  │              message: "Incorrect WiFi password."
  │              (Portal shows error; user re-enters password)
  │
  ├─ SSID not visible (nmcli "no Wi-Fi network" error)
  │      └─► AP_ACTIVE [reason=not_found]
  │              message: "Network not found. Move closer and scan again."
  │
  ├─ nmcli timeout (30 s; router unreachable or Pi too far away)
  │      └─► AP_ACTIVE [reason=timeout]
  │              message: "Connection timed out. Move closer to your router…"
  │
  └─ Other nmcli error (unknown NM error, nmcli not installed)
         └─► AP_ACTIVE [reason=unknown]
                 message: "Could not connect. Please try again."
```

Failure reasons are communicated via `GET /api/v1/wifi/status`:

```json
{
  "state": "ap_active",
  "reason": "authentication_failed",
  "message": "Incorrect WiFi password."
}
```

The Portal uses `message` verbatim — it does not embed NM-specific logic.
This separation means the service layer can improve error classification
without touching the Portal HTML.

---

## Confirmed architecture (original proposal)

These elements of the original M14 proposal were validated without changes:

- **Reuse existing Portal and REST API** — provisioning is a state of the existing Portal, not a second web server.
- **WiFi status state machine** — `unprovisioned → ap_active → connecting → connected` cleanly models the provisioning lifecycle.
- **API surface** — `GET /api/v1/wifi/status`, `GET /api/v1/wifi/networks`, `POST /api/v1/wifi/connect` is the correct minimal API.
- **No SSH, no terminal, no config file editing** — the architectural goal is achievable with the above stack.

---

## Consequences

- dnsmasq is already installed on Pi OS as a dependency of NetworkManager. No new system packages are required for DNS.
- Port 80 binding is moved from M15 into M14. M15's remaining scope becomes end-to-end validation of `http://partybox.local` access and mDNS behaviour after provisioning.
- The companion service requires a polkit rule granting it access to three specific NM D-Bus actions: `network-control`, `wifi.scan`, and `wifi.share.open`. Broader NM access is not granted.
- The dnsmasq wildcard config file is written once during install and is idempotent. It has no effect outside of provisioning mode because the companion-ap connection is never active during normal operation.
- WiFi scanning while in AP mode works on the Pi's BCM43438/BCM2711 chips, but causes a brief radio interruption (~200 ms) per scan. The Portal should debounce scan requests and present results with a "last updated" timestamp rather than scanning continuously.
