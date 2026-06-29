# M14 — Network Provisioning: Hardware Validation Protocol

Manual test scenarios for the WiFi captive-portal provisioning flow.
Run on a production Pi image (`v0.1.0-rc.4` or later) with a real speaker and
a mobile device.

---

## Prerequisites

- Pi image flashed with no prior WiFi credentials (`/etc/NetworkManager/system-connections/`
  should be empty, or image freshly flashed)
- Mobile device with WiFi (iOS or Android)
- A known WPA2 access point you can toggle on/off
- SSH access via Ethernet or USB gadget mode during provisioning tests

---

## Scenario 1 — First boot, no saved networks

**Goal:** Appliance enters provisioning mode automatically on first boot.

1. Flash image; do **not** pre-configure WiFi credentials.
2. Boot the Pi. Wait ~30 s.
3. On your mobile device, scan for WiFi networks.

**Expected:**
- SSID `PartyBox Companion Setup` appears.
- Connecting opens the captive portal (iOS CNA / Android popup).
- Portal shows the provisioning screen (not the dashboard).
- `GET /api/v1/wifi/status` returns `{"state": "ap_active", "ap_ip": "10.42.0.1", ...}`.

---

## Scenario 2 — Correct credentials on first attempt

**Goal:** Entering correct credentials successfully provisions the appliance.

1. Complete Scenario 1 (portal open, provisioning screen visible).
2. Select your home network from the scan list.
3. Enter the correct password and tap **Connect**.

**Expected:**
- Portal shows "Connecting…" spinner.
- `GET /api/v1/wifi/status` transitions: `ap_active` → `connecting` → `connected`.
- `PartyBox Companion Setup` SSID disappears from the mobile WiFi list.
- Pi joins your home network; `http://partybox` (or the Pi's IP) serves the dashboard.
- `GET /api/v1/wifi/status` returns `{"state": "connected", "reason": null, "message": null}`.

---

## Scenario 3 — Incorrect WiFi password

**Goal:** Wrong password shows a clear error message; portal remains accessible for retry.

1. Complete Scenario 1 (portal open, provisioning screen visible).
2. Select your home network.
3. Enter an **incorrect** password and tap **Connect**.

**Expected:**
- Portal shows "Connecting…" briefly.
- After ~5–30 s, the portal shows an error: "Incorrect WiFi password."
- `GET /api/v1/wifi/status` returns:
  ```json
  {"state": "ap_active", "reason": "authentication_failed", "message": "Incorrect WiFi password."}
  ```
- `PartyBox Companion Setup` SSID reappears (AP restored after failure).
- Mobile captive portal remains open or reconnects to the setup AP.

---

## Scenario 4 — Correct password after a failed attempt

**Goal:** Recovering from an auth failure by retrying with the correct password.

1. Complete Scenario 3 (error message shown, AP restored).
2. Re-scan for networks (tap **Scan** in the portal).
3. Select the same network; enter the **correct** password this time.

**Expected:**
- Provisioning succeeds as in Scenario 2.
- `reason` and `message` fields are `null` after success.
- Dashboard loads on the home network.

---

## Scenario 5 — Router unreachable during connection

**Goal:** A network that exists but is out of range (or turned off) produces a clear error, not a hang.

1. Complete Scenario 1.
2. **Disable** your home router (or move the Pi fully out of range).
3. Select the (now invisible) SSID from the scan list, or type it manually.
4. Enter the password and tap **Connect**.

**Expected:**
- `GET /api/v1/wifi/status` shows `connecting` during the attempt.
- After 30 s (the `_CONNECT_TIMEOUT`), status returns:
  ```json
  {"state": "ap_active", "reason": "timeout", "message": "Connection timed out. Move closer to your router and try again."}
  ```
  — **or** —
  ```json
  {"state": "ap_active", "reason": "not_found", "message": "Network not found. Move closer and scan again."}
  ```
  depending on whether NM could see the SSID at all.
- AP is restored; portal is reachable for retry.

---

## Scenario 6 — Restart mid-provisioning

**Goal:** A reboot while the AP is active does not leave stale connections or break the next boot.

1. Complete Scenario 1 (AP active, portal reachable).
2. **Reboot** the Pi (`sudo reboot`) before entering any credentials.
3. Wait ~30 s for the Pi to restart.

**Expected:**
- `PartyBox Companion Setup` SSID reappears after reboot.
- Portal is accessible again.
- No stale `companion-ap` connection entry in NM (`nmcli connection show` should show none,
  or a freshly created one after the service recreates it).

---

## Scenario 7 — SSID with Unicode characters and spaces

**Goal:** SSIDs containing emoji, accents, or spaces parse and connect correctly.

1. Temporarily rename your test router to an SSID containing special characters,
   e.g. `Café WiFi`, `Björns Nät`, or `🎉 Party`.
2. Complete Scenario 1 (portal open).
3. Scan — the special SSID should appear in the list.
4. Select it, enter the correct password, and connect.

**Expected:**
- SSID renders correctly in the scan list.
- `POST /api/v1/wifi/connect` body carries the full Unicode SSID verbatim.
- Connection succeeds; dashboard loads.
- `nmcli connection show --active` shows the correct SSID.

---

## Diagnostics

Useful commands to run over SSH during any scenario:

```bash
# Current NM state
nmcli device status
nmcli connection show --active

# Companion service logs
journalctl -u companion -f

# REST status
curl -s http://localhost/api/v1/wifi/status | python3 -m json.tool

# dnsmasq wildcard-DNS (should resolve any name to 10.42.0.1 while AP is active)
# Run from a device connected to the setup AP:
nslookup example.com 10.42.0.1
```
