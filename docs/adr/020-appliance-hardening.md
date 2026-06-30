# ADR-020 — Appliance Hardening

**Status:** Accepted
**Date:** 2026-06-28
**Milestone:** M13.3

---

## Context

M13.1 and M13.2 produced a working distribution pipeline. M13.3 is a dedicated hardening pass whose goal is to make Companion behave like a dedicated consumer appliance rather than a general-purpose Raspberry Pi.

Pi OS Lite is configured for general interactive use. Several of its defaults — swap enabled, background maintenance services running, GPU memory reserved, Plymouth loaded — are appropriate for a developer machine or server but create unnecessary background activity, SD card wear, and maintenance risk on a long-running embedded appliance.

This ADR documents every hardening decision made in M13.3, including items intentionally deferred to a future milestone.

---

## Design principle

Every change in M13.3 must satisfy at least one of:

- Reduce SD card wear
- Reduce unnecessary background activity
- Improve unattended reliability
- Improve appliance boot behaviour
- Reduce maintenance burden

Changes that do not satisfy at least one goal are out of scope.

---

## Decisions

### SD card longevity

SD cards have limited write endurance. The three largest sources of continuous background writes on a stock Pi OS installation are swap, `/tmp` writes, and journald. Each is addressed below.

#### (a) Disable swap

Pi OS creates a 100 MB swap file at `/var/swap` via the `dphys-swapfile` package and enables it at boot. When memory pressure causes page eviction, pages are written to the swap file — the write amplification from sustained swap activity can shorten SD card life significantly.

**Decision:** Remove `dphys-swapfile` entirely (`apt-get remove --purge`). Disabling it is insufficient because the package's `postinst` script re-enables the swap file on reinstall.

**Assumption:** The appliance (Pi 4 or Pi 5, 4 GB+ RAM) has enough memory for its workload without swap. If this assumption proves false under load, the correct remedy is to reduce the application's memory footprint, not to re-enable swap.

**Trade-off:** If the system genuinely runs out of memory, the kernel will OOM-kill a process rather than page to disk. On a dedicated single-purpose appliance, this is acceptable — an OOM kill produces a visible log event and a clean restart, whereas swap thrashing degrades performance and card life silently.

#### (b) Mount `/tmp` as tmpfs

By default, `/tmp` is on the root filesystem (the SD card). Temporary files — pipe buffers, session state, apt lock files — accumulate there. Mounting `/tmp` as a `tmpfs` moves these writes entirely to RAM.

**Decision:** Add a `tmpfs` entry to `/etc/fstab`:

```
tmpfs /tmp tmpfs rw,noatime,nosuid,nodev,mode=1777,size=64m 0 0
```

The 64 MB cap is sufficient for our workload and prevents a misbehaving process from exhausting RAM.

**Note:** `companion.service` already sets `PrivateTmp=true`, giving the Companion process a private tmpfs `/tmp` independent of this setting. The fstab entry covers other system processes (NetworkManager, Avahi, BlueZ, SSH).

#### (c) Volatile journal

With the Pi OS default (`Storage=auto`), journald writes every log entry from every service to `/var/log/journal` on the SD card. This is a continuous write workload proportional to the volume of logging.

**Decision:** Set `Storage=volatile` in a drop-in at `/etc/systemd/journald.conf.d/appliance.conf`. Logs are kept in `/run/log/journal` (tmpfs-backed, lost on reboot). In-session logs remain available via `journalctl`.

**Trade-off:** Post-crash diagnosis is harder because logs do not survive unexpected reboots. If persistent logging is required for production diagnosis, an operator can place a drop-in at `/etc/systemd/journald.conf.d/persistent.conf` restoring `Storage=persistent` with a size cap. This is not the appliance default.

---

### Service pruning

Pi OS enables several services that provide no value on a dedicated headless appliance.

#### `apt-daily.timer` and `apt-daily-upgrade.timer`

These timers schedule `apt-get update` and `unattended-upgrades` on a randomised daily schedule. For an appliance that is updated by flashing a new image, in-place package upgrades are unacceptable:

- They can silently upgrade packages that the image was tested with, breaking pinned behaviour.
- They consume network bandwidth at unpredictable times.
- They can set a `reboot-required` flag that prompts for a reboot mid-session.
- They can leave the appliance in an inconsistent state if the upgrade fails partway.

**Decision:** Disable both timers and `unattended-upgrades.service`.

#### `man-db.timer`

Rebuilds the `man` page index after package operations. Man pages are never consulted on a production headless appliance.

**Decision:** Disabled.

#### `triggerhappy.service`

The `thd` daemon monitors `/dev/input` for key events and dispatches configurable actions (volume adjustment, power button handling). No input devices are attached to the appliance.

**Decision:** Disabled.

#### `ModemManager.service`

Manages mobile broadband modems (3G/4G/5G). Not installed on Pi OS Lite, but included in the disable list as a precaution in case a future dependency pulls it in.

**Decision:** Disabled if present.

---

### Headless boot

#### GPU memory split

The VideoCore GPU on Pi 4 permanently reserves memory from the ARM Linux pool, even when no display is attached. The Pi OS default is 76 MB. The minimum supported by the KMS driver is 16 MB.

**Decision:** Set `gpu_mem=16` in `/boot/firmware/config.txt`. This frees approximately 60 MB of RAM for Linux.

**Note on Pi 5:** The Pi 5's display output is handled by the RP1 chip rather than VideoCore; `gpu_mem` has different semantics. Setting it to 16 should be harmless (it may be ignored), but this has not been validated on Pi 5 hardware.

#### Splash screen removal

Pi OS loads Plymouth during boot to display a graphical splash screen. On a headless appliance with no HDMI connection, Plymouth loads a framebuffer driver and renders a splash that nobody sees.

**Decision:** Two changes remove all splash behaviour:

1. `disable_splash=1` in `/boot/firmware/config.txt` — removes the Pi firmware's rainbow display shown before the kernel loads.
2. Remove `splash` and `plymouth.ignore-serial-consoles` from `/boot/firmware/cmdline.txt` — disables Plymouth in the kernel boot sequence.

`quiet` is retained in `cmdline.txt`. Boot messages are suppressed on the HDMI console (appropriate for a production appliance) but remain visible on the UART serial port (`/dev/ttyAMA0` at 115200 baud), which is the correct interface for low-level debugging.

---

### WiFi power management

Pi OS enables WiFi power saving by default. NetworkManager sets `wifi.powersave = 3` (enabled), which allows the driver to put the adapter into a low-power state between packets. The driver wakes on incoming traffic, but the wake latency is non-deterministic and can delay multicast packet delivery.

**Decision:** Disable WiFi power saving (`wifi.powersave = 2`) via a NetworkManager drop-in at `/etc/NetworkManager/conf.d/wifi-powersave-off.conf`.

**Rationale:**

The appliance prioritises low latency, stable multicast, and a responsive Portal over minimal power consumption. Three operations are sensitive to WiFi power-save latency:

- **mDNS (Avahi):** mDNS uses multicast UDP. If the adapter is asleep when a multicast packet arrives, the packet may be dropped or delivered late, causing `partybox.local` to fail to resolve intermittently.
- **A2DP audio:** Bluetooth audio is carried over the air by the speaker, not WiFi, but the Portal's real-time status updates (WebSocket) are sensitive to TCP latency spikes.
- **Portal responsiveness:** HTTP requests from a browser on the same network see higher tail latency when the adapter wakes up for each request.

The additional power draw from keeping the WiFi adapter continuously active is negligible compared to the PartyBox speaker itself, which is the dominant power consumer in any realistic deployment.

**Honest caveat:** WiFi power saving was suspected as a root cause of observed connectivity issues during development. However, we never collected definitive evidence that power saving was responsible — the issues resolved after other changes were made in parallel. The decision to disable it is an engineering preference (low latency and stable multicast are inherently valuable on a network appliance) rather than a proven fix. If a future operator needs to minimise power consumption, re-enabling power saving via `/etc/NetworkManager/conf.d/` is straightforward and can be evaluated against their specific network environment.

---

## Deferred items

The following hardening improvements have clear potential value but require hardware validation before becoming the default appliance configuration.

### Hardware watchdog

Linux exposes the SoC's hardware watchdog at `/dev/watchdog`. If the kernel is not kicked at the configured interval, the SoC resets the board. systemd can kick the watchdog on behalf of the whole system via `RuntimeWatchdogSec` in `system.conf`.

**Expected benefit:** Automatic recovery from system hangs (kernel panics, deadlocks, unresponsive init) without manual power-cycling. Critical for remote or unattended deployments.

**Risk:** If `RuntimeWatchdogSec` is set too short, the watchdog fires during legitimate long operations (SD card I/O bursts, large apt operations, heavy logging). systemd kicks the watchdog from PID 1's main loop; the concern is whether any operation can pause the main loop long enough to miss a keep-alive.

**Validation required:** Run the appliance under normal and stress conditions (e.g. a large apt cache update, SD card heavy I/O), confirm the watchdog is being kicked reliably, and choose a timeout with adequate safety margin. The BCM2711 (Pi 4) watchdog supports a maximum timeout of approximately 15 seconds.

### Root filesystem `noatime`

Adding `noatime` to the root filesystem mount options prevents the kernel from updating the access timestamp on every file read. This eliminates a write-per-read on the SD card.

**Expected benefit:** Reduced write amplification for read-heavy workloads. In practice, `relatime` (the kernel default since 2.6.30) already limits atime updates to once per day unless the file has been modified more recently than its atime. The marginal improvement of `noatime` over `relatime` is small for most workloads.

**Risk:** Some tools use atime to detect "files not accessed recently" (log rotation strategies, backup utilities). None of these apply to our appliance, but the risk of unexpected breakage warrants a full boot test rather than adding it without validation.

**Validation required:** A full boot test (not just the chroot smoke test) confirming that Pi OS init, NetworkManager, BlueZ, Avahi, and Companion all start correctly with `noatime` on the root mount.

### Bluetooth plugin restrictions

BlueZ can be started with `--noplugin=<list>` to disable plugin categories. Unused plugins (e.g. `network`, `sap`, `input`) add D-Bus endpoints and background activity with no benefit.

**Expected benefit:** Reduced BlueZ memory footprint, fewer active D-Bus endpoints, smaller attack surface for BLE security issues.

**Risk:** Incorrect plugin exclusion could prevent A2DP audio routing or BLE GATT operation. The complete list of plugins required for our use case (BLE GATT control + A2DP source) must be validated on hardware.

**Validation required:** Confirm that the chosen plugin exclusion list does not affect BLE device scanning, GATT characteristic write/notify, or A2DP audio routing — tested with a connected JBL PartyBox.

---

## Audio stack

### Design principle

> The appliance should use the simplest audio architecture that satisfies every supported playback service.

No audio architecture decision is made in M13.3. The decision is explicitly deferred until all playback milestones — including AirPlay via shairport-sync — are complete, because the requirements of AirPlay are not yet known.

### Current state

PipeWire, `pipewire-pulse`, `libspa-0.2-bluetooth`, and WirePlumber are installed in the image (see `install.sh` section 1). They are not actively configured for any specific audio routing role. This preserves both paths without committing to either.

### Alternatives under consideration

**ALSA (Advanced Linux Sound Architecture)**

Direct kernel audio interface. No audio server, no session manager. librespot supports ALSA natively (`--backend alsa`). shairport-sync supports ALSA (`--with-alsa`).

ALSA does not natively route audio to Bluetooth A2DP. It requires an intermediate layer:
- `bluealsa` — a lightweight BlueZ-to-ALSA bridge maintained alongside the BlueZ project. Exposes the Bluetooth sink as a standard ALSA device. Low overhead, minimal configuration.
- `snd-aloop` — kernel loopback module. More complex, not clearly beneficial here.

If librespot and shairport-sync can each write to the A2DP sink via bluealsa without conflict (e.g. via ALSA's dmix for soft mixing), ALSA is the lowest-complexity solution.

**PipeWire**

A modern audio/video server. Handles Bluetooth A2DP natively via its BlueZ backend and routes between multiple audio sources with session management (WirePlumber). Already installed.

PipeWire is the natural choice if:
- Multiple playback clients need simultaneous or exclusive-access management.
- The A2DP session should be maintained independently of individual clients.
- AirPlay requires features that ALSA cannot provide.

**PulseAudio**

Older audio server. Still widely supported but actively being replaced by PipeWire. Not considered for new deployments.

### Decision criteria

The audio architecture decision will be made during the AirPlay milestone when full requirements are known. Key questions:

1. Does shairport-sync require PipeWire, PulseAudio, or does ALSA + bluealsa suffice?
2. Can librespot and shairport-sync share the same A2DP sink without conflict?
3. Does PipeWire provide A2DP audio quality or latency advantages over ALSA + bluealsa?

---

## Consequences

- Unnecessary background activity (apt, man-db, triggerhappy) is eliminated
- SD card write amplification from swap, `/tmp`, and journald is eliminated or capped
- Boot is faster and cleaner (no Plymouth splash, no firmware rainbow, minimal GPU allocation)
- Hardware watchdog, `noatime`, and Bluetooth plugin hardening are explicitly deferred with documented validation requirements
- Audio architecture remains flexible until AirPlay requirements are known
- The image ships with a default `pi` user (password `raspberry`, `sudo` group) and `openssh-server` enabled. SSH is the primary administration interface for a headless appliance, and without a default user the image is completely inaccessible on first boot. The well-known default credentials are an acceptable trade-off during development but are a known security risk on a network-connected device. **This must be re-evaluated before v1.0**: options include first-boot credential randomisation, requiring a password change before SSH access is granted, or switching to key-only authentication with a setup flow that installs the user's public key.
- All hardening decisions are recorded here; future contributors will understand these are deliberate product choices rather than generic Linux tuning
