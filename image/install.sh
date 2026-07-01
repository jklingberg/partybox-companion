#!/usr/bin/env bash
# PartyBox Companion — appliance setup script
#
# This is the single authoritative implementation of how Companion is installed.
# All installation contexts — CI image build, manual Pi install, future test
# harnesses — invoke this script. See ADR-019 for the architectural rationale.
#
# Invocation contexts:
#
#   CI image build (arm-runner-action copies the repo to /opt/partybox-src):
#     PARTYBOX_SRC_DIR=/opt/partybox-src bash image/install.sh
#
#   Manual install on a running Pi (developers and advanced users only;
#   the appliance image is the primary supported deployment — see image/README.md):
#     sudo PARTYBOX_SRC_DIR=$(pwd) bash image/install.sh
#
# Environment variables:
#   PARTYBOX_SRC_DIR     Path to the repository root.
#                        Defaults to the parent of this script.
#   COMPANION_VERSION    Version string embedded in /etc/partybox-companion/version.
#                        Defaults to "dev".

set -euo pipefail

PARTYBOX_SRC_DIR="${PARTYBOX_SRC_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
COMPANION_VERSION="${COMPANION_VERSION:-dev}"
INSTALL_PREFIX=/opt/partybox-companion
UV_VERSION=0.11.24

log() { printf '==> %s\n' "$*" >&2; }

export DEBIAN_FRONTEND=noninteractive

# ──────────────────────────────────────────────────────────────────────────────
# 1. System packages
#
# All apt sources are registered before the single apt-get update so that
# only one index fetch is required. A second apt-get update (to pick up a
# newly added repo) can fail with ENOMEM in QEMU after the heavy package
# install has consumed most available memory.
# ──────────────────────────────────────────────────────────────────────────────
log "Adding raspotify apt repository"
curl -fsSL https://dtcooper.github.io/raspotify/key.asc \
    | gpg --dearmor -o /usr/share/keyrings/raspotify.gpg
echo "deb [signed-by=/usr/share/keyrings/raspotify.gpg] https://dtcooper.github.io/raspotify/ raspotify main" \
    > /etc/apt/sources.list.d/raspotify.list

log "Installing system packages"
apt-get update -qq
apt-get install -y --no-install-recommends \
    pipewire \
    pipewire-pulse \
    libspa-0.2-bluetooth \
    wireplumber \
    bluez \
    avahi-daemon \
    openssh-server \
    python3 \
    curl \
    ca-certificates \
    gnupg

# ──────────────────────────────────────────────────────────────────────────────
# 2. librespot (via raspotify — prebuilt ARM binary)
#
# Companion owns the librespot lifecycle (ADR-016). The raspotify.service unit
# is disabled immediately — it is a conflicting orchestrator and must not run.
# The raspotify repo was added to apt sources in section 1 (before apt-get
# update), so no second update is needed here.
# ──────────────────────────────────────────────────────────────────────────────
log "Installing librespot"
apt-get install -y raspotify
systemctl disable raspotify 2>/dev/null || true

# raspotify installs the librespot binary to /usr/bin/librespot. Symlink it
# to /usr/local/bin so the documented layout (/usr/local/bin/librespot) and
# SpotifyService's shutil.which("librespot") PATH lookup both work.
ln -sf "$(command -v librespot)" /usr/local/bin/librespot

# ──────────────────────────────────────────────────────────────────────────────
# 3. companion user
#
# System user with no home directory and no login shell. ProtectHome=true in
# the service unit makes /home inaccessible at runtime anyway. Persistent state
# lives in /var/lib/companion (StateDirectory=) and runtime artefacts such as
# PipeWire sockets live in /run/companion (RuntimeDirectory=) — both managed by
# systemd, not by a home directory. See ADR-019 for the full rationale.
# ──────────────────────────────────────────────────────────────────────────────
log "Creating companion user"
if ! id companion &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin companion
fi
usermod -aG bluetooth companion

# ──────────────────────────────────────────────────────────────────────────────
# 4. uv (Python toolchain)
#
# Pinned to an exact version for release engineering reasons: every image built
# from the same git tag must produce an identical result. An unpinned "latest"
# download would silently update between releases, introducing a variable that
# cannot be reproduced from the tag alone. The pin is intentional and is treated
# like any other dependency — updating it is an explicit, reviewable change.
#
# The musl-linked binary runs without glibc version constraints and is installed
# system-wide.
#
# To update: change UV_VERSION, push a pre-release tag, verify with: uv --version.
# TODO: add SHA256 verification alongside the next version bump. The checksum
# file is published at the same URL with a .sha256 suffix, e.g.:
#   https://github.com/astral-sh/uv/releases/download/X.Y.Z/uv-ARCH.tar.gz.sha256
# ──────────────────────────────────────────────────────────────────────────────
log "Installing uv ${UV_VERSION}"
ARCH=$(dpkg --print-architecture)
case "${ARCH}" in
    arm64) UV_ARCH=aarch64-unknown-linux-musl ;;
    armhf) UV_ARCH=arm-unknown-linux-musleabihf ;;
    amd64) UV_ARCH=x86_64-unknown-linux-musl ;;
    *) echo "Unsupported architecture: ${ARCH}" >&2; exit 1 ;;
esac
UV_TGZ="/tmp/uv-${UV_VERSION}.tar.gz"
curl -fsSL \
    "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${UV_ARCH}.tar.gz" \
    -o "${UV_TGZ}"
tar -xzf "${UV_TGZ}" -C /tmp
install -m 755 "/tmp/uv-${UV_ARCH}/uv" /usr/local/bin/uv
rm -f "${UV_TGZ}" && rm -rf "/tmp/uv-${UV_ARCH}"

# ──────────────────────────────────────────────────────────────────────────────
# 5. Python venv + Companion packages
#
# Installs partybox, partyboxd, and companion from the repository source,
# with ALL transitive dependency versions pinned to uv.lock for reproducible
# image builds. Two builds from the same tag always produce identical installs.
#
# UV_PROJECT_ENVIRONMENT redirects the venv from the workspace default (.venv)
# to our production path. --frozen refuses to update uv.lock (fail loudly if
# it is out of date). --no-editable copies workspace package source into
# site-packages rather than creating .pth symlinks — the source tree is deleted
# after install so symlinks would be dangling. --no-dev excludes test/lint deps.
# ──────────────────────────────────────────────────────────────────────────────
log "Installing partybox-companion (locked)"
(
    cd "${PARTYBOX_SRC_DIR}"
    # hatch-vcs derives the version from git tags. The QEMU chroot used for
    # image builds has no git history, so without this hint it falls back to
    # 0.0.0.dev0. SETUPTOOLS_SCM_PRETEND_VERSION is the standard override
    # that tells hatch-vcs to use the supplied version string verbatim.
    export SETUPTOOLS_SCM_PRETEND_VERSION="${COMPANION_VERSION}"
    UV_PROJECT_ENVIRONMENT="${INSTALL_PREFIX}" \
        uv sync --frozen --no-dev --no-editable
)

ln -sf "${INSTALL_PREFIX}/bin/partybox-companion" /usr/local/bin/partybox-companion

# ──────────────────────────────────────────────────────────────────────────────
# 6. systemd service
# ──────────────────────────────────────────────────────────────────────────────
log "Installing companion.service"
install -m 0644 \
    "${PARTYBOX_SRC_DIR}/system/systemd/companion.service" \
    /lib/systemd/system/companion.service

mkdir -p /etc/companion
install -m 0644 \
    "${PARTYBOX_SRC_DIR}/system/systemd/companion.env" \
    /etc/companion/companion.env

# systemctl enable creates the want-symlink; works both in chroot and on a live
# system because it only creates files — it does not start the service.
systemctl enable companion 2>/dev/null || \
    ln -sf /lib/systemd/system/companion.service \
        /etc/systemd/system/multi-user.target.wants/companion.service

# ──────────────────────────────────────────────────────────────────────────────
# 7. Avahi (mDNS — partybox.local)
# ──────────────────────────────────────────────────────────────────────────────
log "Installing Avahi service record"
install -m 0644 \
    "${PARTYBOX_SRC_DIR}/system/avahi/partyboxd.service" \
    /etc/avahi/services/partyboxd.service

# ──────────────────────────────────────────────────────────────────────────────
# 8. BlueZ — auto-enable Bluetooth adapter on boot (see system/README.md)
# ──────────────────────────────────────────────────────────────────────────────
log "Configuring BlueZ (AutoEnable=true, Channels=1)"
BLUEZ_CONF=/etc/bluetooth/main.conf
if [ -f "${BLUEZ_CONF}" ]; then
    # Uncomment the existing AutoEnable line if present as a comment
    sed -i 's/^#\s*AutoEnable\s*=.*/AutoEnable=true/' "${BLUEZ_CONF}"
    # Append [Policy] section if AutoEnable is still absent
    if ! grep -q '^AutoEnable=true' "${BLUEZ_CONF}"; then
        printf '\n[Policy]\nAutoEnable=true\n' >> "${BLUEZ_CONF}"
    fi
    # Disable EATT (Enhanced ATT channels). The JBL PartyBox 520 advertises
    # EATT support but rejects EATT connections without an encrypted LE link,
    # triggering SMP pairing which the speaker rejects. Setting Channels=1
    # forces BlueZ to use only the standard ATT bearer (CID 0x0004), which
    # works without encryption for the vendor GATT service.
    sed -i 's/^#\s*Channels\s*=.*/Channels = 1/' "${BLUEZ_CONF}"
    if ! grep -q '^Channels\s*=' "${BLUEZ_CONF}"; then
        printf '\n[GATT]\nChannels = 1\n' >> "${BLUEZ_CONF}"
    fi
else
    mkdir -p /etc/bluetooth
    printf '[Policy]\nAutoEnable=true\n\n[GATT]\nChannels = 1\n' > "${BLUEZ_CONF}"
fi

# ──────────────────────────────────────────────────────────────────────────────
# 9. WiFi power management (reduces mDNS unreliability during A2DP streaming)
# ──────────────────────────────────────────────────────────────────────────────
log "Disabling WiFi power management"
mkdir -p /etc/NetworkManager/conf.d
install -m 0644 \
    "${PARTYBOX_SRC_DIR}/image/config/wifi-powersave.conf" \
    /etc/NetworkManager/conf.d/wifi-powersave-off.conf

# ──────────────────────────────────────────────────────────────────────────────
# 9a. Captive portal DNS (provisioning mode)
#
# NetworkManager's internal dnsmasq (started for AP shared connections) reads
# drop-ins from /etc/NetworkManager/dnsmasq-shared.d/. The captive.conf file
# sets address=/#/10.42.0.1 so that every hostname resolves to the AP gateway,
# causing iOS/Android captive portal probes to land on the Companion Portal.
# ──────────────────────────────────────────────────────────────────────────────
log "Installing captive portal DNS drop-in"
mkdir -p /etc/NetworkManager/dnsmasq-shared.d
install -m 0644 \
    "${PARTYBOX_SRC_DIR}/image/config/nm-captive.conf" \
    /etc/NetworkManager/dnsmasq-shared.d/captive.conf

# ──────────────────────────────────────────────────────────────────────────────
# 9b. polkit rule — companion user → NetworkManager D-Bus access
#
# ProvisioningService drives NM via nmcli, which issues D-Bus calls. By default
# non-root users may only inspect NM state; modifying connections or bringing
# interfaces up/down requires authorization. This rule grants the companion
# system user unconditional (auth_admin_keep-free) access to all NM actions so
# that the provisioning AP lifecycle can run without a password prompt and
# without root.
# ──────────────────────────────────────────────────────────────────────────────
log "Installing polkit rule for companion → NetworkManager"
mkdir -p /etc/polkit-1/rules.d
cat > /etc/polkit-1/rules.d/51-companion-nm.rules << 'POLKIT_EOF'
// Grant the companion system user full access to all NetworkManager D-Bus
// actions. The companion user is a no-login system account (no shell, no
// home directory) whose sole purpose is running the companion service.
// Restricting to a specific action ID subset is fragile: NM checks
// different action IDs for different operations (connection-add, wifi-scan,
// shared-AP bring-up, STA connect) and the exact set varies across NM
// versions. A prefix match on the org.freedesktop.NetworkManager namespace
// is the correct scope for a dedicated system user.
polkit.addRule(function(action, subject) {
    if (action.id.indexOf("org.freedesktop.NetworkManager.") === 0
            && subject.user === "companion") {
        return polkit.Result.YES;
    }
});
POLKIT_EOF
chmod 0644 /etc/polkit-1/rules.d/51-companion-nm.rules

# ──────────────────────────────────────────────────────────────────────────────
# 10. Hostname
#
# "partybox" is the appliance default — it determines the mDNS address
# (partybox.local) and the router hostname. It is not a permanent value.
# A future Portal rename feature should call: hostnamectl set-hostname <name>
# and restart avahi-daemon. /etc/hostname is a standard file; nothing here
# prevents that.
# ──────────────────────────────────────────────────────────────────────────────
log "Setting hostname to 'partybox'"
echo "partybox" > /etc/hostname
sed -i '/^127\.0\.1\.1/d' /etc/hosts
echo "127.0.1.1	partybox" >> /etc/hosts

# ──────────────────────────────────────────────────────────────────────────────
# 10a. NetworkManager WiFi radio state
#
# The base Pi OS image (and the QEMU build environment) may persist
# WirelessEnabled=false in /var/lib/NetworkManager/NetworkManager.state.
# NM reads this file on startup and keeps the radio disabled, preventing
# the provisioning AP from ever being created. Reset it here so the image
# always boots with the WiFi radio enabled.
# ──────────────────────────────────────────────────────────────────────────────
log "Ensuring NetworkManager WiFi radio is enabled on boot"
mkdir -p /var/lib/NetworkManager
cat > /var/lib/NetworkManager/NetworkManager.state << 'NM_STATE_EOF'
[main]
NetworkingEnabled=true
WirelessEnabled=true
WWANEnabled=true
NM_STATE_EOF

# ──────────────────────────────────────────────────────────────────────────────
# 10b. SSH access
#
# Creates a default login user and enables the SSH daemon. SSH is the primary
# administration interface for a headless appliance.
#
# Pi OS Bookworm ships openssh-server but disables it because the default pi
# user was removed in Bookworm. We restore both here.
#
# Default credentials: pi / raspberry
# Change the password after first login:  ssh pi@partybox.local  &&  passwd
# ──────────────────────────────────────────────────────────────────────────────
log "Creating default SSH user (pi)"
if ! id pi &>/dev/null; then
    useradd -m -s /bin/bash -G sudo pi
fi
echo "pi:raspberry" | chpasswd

log "Enabling SSH daemon"
systemctl enable ssh

# Bookworm's default sshd_config uses PasswordAuthentication prohibit-password.
# Override via a drop-in so that password login works for the pi user.
mkdir -p /etc/ssh/sshd_config.d
printf 'PasswordAuthentication yes\n' \
    > /etc/ssh/sshd_config.d/10-partybox.conf

# ──────────────────────────────────────────────────────────────────────────────
# 10c. PipeWire user session for the pi user (A2DP audio pipeline)
#
# Spotify Connect (librespot) outputs audio via the PulseAudio-compatible
# socket that PipeWire-pulse creates in the pi user's XDG_RUNTIME_DIR.
# The main wireplumber.service (loaded with bluetooth.lua) registers A2DP
# media endpoints with BlueZ and creates PipeWire sink nodes for connected
# Bluetooth speakers — no separate wireplumber-bluetooth instance is needed.
#
# Two steps make this work on a headless appliance with no active login:
#
# (1) loginctl enable-linger: tells systemd to start and maintain the pi user's
#     session (user@1000.service) at boot, keeping PipeWire and WirePlumber
#     alive without requiring an SSH login. Implemented by creating the linger
#     marker file directly (loginctl is not available in a chroot).
#
# (2) User service symlinks: the equivalent of `systemctl --user enable` for
#     each service. Created manually because systemctl --user cannot run in a
#     QEMU chroot without a live systemd user instance.
# ──────────────────────────────────────────────────────────────────────────────
log "Configuring PipeWire user session for pi (audio pipeline)"

# (1) Enable linger — creates /var/lib/systemd/linger/pi
mkdir -p /var/lib/systemd/linger
touch /var/lib/systemd/linger/pi

# (2) Enable PipeWire, PipeWire-Pulse, and WirePlumber user services
mkdir -p /home/pi/.config/systemd/user/default.target.wants
for svc in pipewire.socket pipewire-pulse.socket wireplumber.service; do
    ln -sf "/usr/lib/systemd/user/${svc}" \
        "/home/pi/.config/systemd/user/default.target.wants/${svc}"
done

chown -R pi:pi /home/pi/.config

# ──────────────────────────────────────────────────────────────────────────────
# 11. SD card longevity
#
# Three changes reduce the write volume that normal operation imposes on the
# SD card. Each one targets a different write source.
#
# (a) Swap. Pi OS creates a 100 MB swap file at /var/swap by default. Swap
#     writes occur whenever memory pressure evicts pages; write amplification
#     from swap thrashing is the fastest way to wear out an SD card. An
#     appliance should be sized to avoid swap; if RAM is genuinely exhausted
#     an OOM kill is preferable to card thrashing. dphys-swapfile is removed
#     entirely — disabling is not sufficient because its postinst re-enables
#     the swap file on reinstall.
#
# (b) /tmp on tmpfs. Temporary files go to RAM instead of the SD card.
#     The 64 MB cap is sufficient for our workload. Note that companion.service
#     has PrivateTmp=true, giving the Companion process its own private tmpfs
#     regardless; this entry covers other system processes.
#
# (c) Volatile journal. With the Pi OS default (Storage=auto), journald writes
#     every log entry to /var/log/journal on the SD card. Setting
#     Storage=volatile moves the journal to /run/log/journal (tmpfs-backed).
#     Logs are available for the current session via journalctl but lost on
#     reboot. See image/config/journald-appliance.conf and ADR-020.
# ──────────────────────────────────────────────────────────────────────────────
log "Configuring SD card longevity"

# (a) Remove swap
apt-get remove -y --purge dphys-swapfile 2>/dev/null || true
rm -f /var/swap

# (b) /tmp as tmpfs
if ! grep -q '^tmpfs /tmp' /etc/fstab; then
    echo "tmpfs /tmp tmpfs rw,noatime,nosuid,nodev,mode=1777,size=64m 0 0" >> /etc/fstab
fi

# (c) Volatile journal
mkdir -p /etc/systemd/journald.conf.d
install -m 0644 \
    "${PARTYBOX_SRC_DIR}/image/config/journald-appliance.conf" \
    /etc/systemd/journald.conf.d/appliance.conf

# ──────────────────────────────────────────────────────────────────────────────
# 12. Disable unnecessary background services
#
# Pi OS enables several services appropriate for a general-purpose machine but
# providing no value — and causing unnecessary background activity — on a
# dedicated headless appliance. See ADR-020 for the full rationale.
#
# apt-daily.timer / apt-daily-upgrade.timer / unattended-upgrades.service
#   Schedule apt-get update and unattended package upgrades. An appliance is
#   updated by flashing a new image. In-place upgrades are unacceptable: they
#   can break pinned software, consume network bandwidth unpredictably, and in
#   the worst case trigger a reboot-required flag mid-session.
#
# man-db.timer
#   Rebuilds the man page index after package changes. Man pages are never
#   consulted on a production headless appliance.
#
# triggerhappy.service (thd)
#   Monitors /dev/input for key events and dispatches configurable actions
#   (volume keys, power buttons). No input devices are attached.
#
# ModemManager.service
#   Manages mobile broadband modems. Not installed on Pi OS Lite, but
#   included here in case a dependency pulls it in.
#
# userconf-pi
#   Pi OS Bookworm's first-user-setup mechanism. It displays an SSH banner
#   ("SSH may not work until a valid user has been set up — rptl.io/newuser")
#   on every login. Our install.sh creates the pi and companion users
#   directly, making this mechanism both inapplicable and misleading.
#   The package postinst creates /etc/ssh/sshd_config.d/rename_user.conf
#   outside of dpkg's file manifest, so purge alone does not remove it —
#   the explicit rm is required.
# ──────────────────────────────────────────────────────────────────────────────
log "Disabling unnecessary background services"
for svc in \
    apt-daily.timer \
    apt-daily-upgrade.timer \
    unattended-upgrades.service \
    man-db.timer \
    triggerhappy.service \
    ModemManager.service
do
    systemctl disable "${svc}" 2>/dev/null || true
done

apt-get remove -y --purge userconf-pi 2>/dev/null || true
rm -f /etc/ssh/sshd_config.d/rename_user.conf

# ──────────────────────────────────────────────────────────────────────────────
# 13. Headless boot
#
# Two adjustments make the boot cleaner and more efficient for a permanently
# headless appliance. See ADR-020 for the rationale.
#
# (a) GPU memory (gpu_mem=16). The VideoCore GPU on Pi 4 permanently reserves
#     memory even with no display attached. The minimum is 16 MB; setting it
#     explicitly frees ~60 MB that would otherwise be unavailable to Linux.
#     disable_splash=1 removes the firmware rainbow shown before the kernel
#     loads. On Pi 5, gpu_mem semantics differ; the values are harmless.
#
# (b) Plymouth splash removal. Pi OS runs Plymouth during boot to show a
#     graphical splash. On a headless appliance, Plymouth loads for no visible
#     result. "splash" and "plymouth.ignore-serial-consoles" are removed from
#     the kernel command line. "quiet" is retained — boot messages are
#     suppressed on the console (appropriate for production) and remain
#     visible on the UART serial port for debugging.
# ──────────────────────────────────────────────────────────────────────────────
log "Configuring headless boot"

CONFIG_TXT="/boot/firmware/config.txt"
CMDLINE_TXT="/boot/firmware/cmdline.txt"

# (a) GPU memory + firmware splash
if [ -f "${CONFIG_TXT}" ]; then
    if grep -q '^gpu_mem=' "${CONFIG_TXT}"; then
        sed -i 's/^gpu_mem=.*/gpu_mem=16/' "${CONFIG_TXT}"
    else
        echo "gpu_mem=16" >> "${CONFIG_TXT}"
    fi
    grep -q '^disable_splash=' "${CONFIG_TXT}" || echo "disable_splash=1" >> "${CONFIG_TXT}"
else
    log "Warning: ${CONFIG_TXT} not found — skipping GPU memory and splash config"
fi

# (b) Kernel command line: remove Plymouth parameters
if [ -f "${CMDLINE_TXT}" ]; then
    sed -i 's/\bsplash\b//g; s/\bplymouth\.ignore-serial-consoles\b//g' "${CMDLINE_TXT}"
    sed -i 's/  */ /g; s/^ //; s/ $//' "${CMDLINE_TXT}"
fi

# ──────────────────────────────────────────────────────────────────────────────
# 14. Version record + MOTD
# ──────────────────────────────────────────────────────────────────────────────
log "Recording version ${COMPANION_VERSION}"
mkdir -p /etc/partybox-companion
echo "${COMPANION_VERSION}" > /etc/partybox-companion/version

log "Installing MOTD"
sed "s/COMPANION_VERSION/${COMPANION_VERSION}/" \
    "${PARTYBOX_SRC_DIR}/image/config/motd" > /etc/motd

# ──────────────────────────────────────────────────────────────────────────────
# 15. Clean up
# ──────────────────────────────────────────────────────────────────────────────
log "Cleaning up"
apt-get autoremove -y --purge
apt-get clean
rm -rf /var/lib/apt/lists/*

# Remove package download caches accumulated during the image build.
# uv caches wheels and source distributions under /root/.cache/uv; pip may
# leave a cache under /root/.cache/pip if any pip invocations occurred.
rm -rf /root/.cache/uv /root/.cache/pip

# Clear build logs and bash history that accumulated during the customisation.
find /var/log -type f \( -name "*.log" -o -name "*.log.*" \) -delete 2>/dev/null || true
truncate -s 0 /root/.bash_history 2>/dev/null || true

# Remove the source tree if it was copied into the image by CI (not the repo
# root). The CI build copies it to /opt/partybox-src; running manually, the
# PARTYBOX_SRC_DIR is outside the image so this guard prevents accidental deletion.
if [ "${PARTYBOX_SRC_DIR}" = "/opt/partybox-src" ]; then
    log "Removing source tree (CI mode)"
    rm -rf /opt/partybox-src
fi

log "Done: partybox-companion ${COMPANION_VERSION} installed"
log "  Venv:    ${INSTALL_PREFIX}"
log "  Binary:  /usr/local/bin/partybox-companion"
log "  Service: companion.service (enabled, starts on next boot)"
log "  Portal:  http://partybox.local (after boot)"
