#!/usr/bin/env bash
# PartyBox Companion — appliance setup script
#
# Runs inside the Raspberry Pi OS Lite ARM64 image during the CI image build.
# Can also be run directly on a freshly flashed Pi for manual installation.
#
# Usage (image build — source dir is copied in by arm-runner-action):
#   PARTYBOX_SRC_DIR=/opt/partybox-src bash image/install.sh
#
# Usage (manual — run from the repository root):
#   sudo PARTYBOX_SRC_DIR=$(pwd) bash image/install.sh
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

log() { printf '==> %s\n' "$*" >&2; }

export DEBIAN_FRONTEND=noninteractive

# ──────────────────────────────────────────────────────────────────────────────
# 1. System packages
# ──────────────────────────────────────────────────────────────────────────────
log "Installing system packages"
apt-get update -qq
apt-get install -y --no-install-recommends \
    pipewire \
    pipewire-pulse \
    libspa-0.2-bluetooth \
    wireplumber \
    bluez \
    avahi-daemon \
    python3 \
    curl \
    ca-certificates \
    gnupg

# ──────────────────────────────────────────────────────────────────────────────
# 2. librespot (via raspotify — prebuilt ARM binary)
#
# Companion owns the librespot lifecycle (ADR-016). The raspotify.service unit
# is disabled immediately — it is a conflicting orchestrator and must not run.
# ──────────────────────────────────────────────────────────────────────────────
log "Installing librespot"
curl -fsSL https://dtcooper.github.io/raspotify/key.asc \
    | gpg --dearmor -o /usr/share/keyrings/raspotify.gpg
echo "deb [signed-by=/usr/share/keyrings/raspotify.gpg] https://dtcooper.github.io/raspotify/ raspotify main" \
    > /etc/apt/sources.list.d/raspotify.list
apt-get update -qq
apt-get install -y raspotify
systemctl disable raspotify 2>/dev/null || true

# ──────────────────────────────────────────────────────────────────────────────
# 3. companion user
# ──────────────────────────────────────────────────────────────────────────────
log "Creating companion user"
if ! id companion &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin companion
fi
usermod -aG bluetooth companion

# ──────────────────────────────────────────────────────────────────────────────
# 4. uv (Python toolchain)
#
# The install script detects the host architecture and fetches the matching
# musl-linked binary, which runs without glibc version constraints.
# Installs to /usr/local/bin so it is available system-wide.
# ──────────────────────────────────────────────────────────────────────────────
log "Installing uv"
curl -LsSf https://astral.sh/uv/install.sh \
    | UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh

# ──────────────────────────────────────────────────────────────────────────────
# 5. Python venv + Companion packages
#
# Installs partybox, partyboxd, and companion from the repository source.
# uv resolves the workspace graph and installs all transitive dependencies.
# The venv at INSTALL_PREFIX matches the production layout in ADR-017.
# ──────────────────────────────────────────────────────────────────────────────
log "Creating venv at ${INSTALL_PREFIX}"
uv venv "${INSTALL_PREFIX}" --python python3

log "Installing partybox-companion"
uv pip install \
    --python "${INSTALL_PREFIX}/bin/python" \
    "${PARTYBOX_SRC_DIR}/packages/partybox" \
    "${PARTYBOX_SRC_DIR}/packages/partyboxd" \
    "${PARTYBOX_SRC_DIR}/packages/companion"

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
log "Configuring BlueZ (AutoEnable=true)"
BLUEZ_CONF=/etc/bluetooth/main.conf
if [ -f "${BLUEZ_CONF}" ]; then
    # Uncomment the existing AutoEnable line if present as a comment
    sed -i 's/^#\s*AutoEnable\s*=.*/AutoEnable=true/' "${BLUEZ_CONF}"
    # Append [Policy] section if AutoEnable is still absent
    if ! grep -q '^AutoEnable=true' "${BLUEZ_CONF}"; then
        printf '\n[Policy]\nAutoEnable=true\n' >> "${BLUEZ_CONF}"
    fi
else
    mkdir -p /etc/bluetooth
    printf '[Policy]\nAutoEnable=true\n' > "${BLUEZ_CONF}"
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
# 10. Hostname
# ──────────────────────────────────────────────────────────────────────────────
log "Setting hostname to 'partybox'"
echo "partybox" > /etc/hostname
sed -i '/^127\.0\.1\.1/d' /etc/hosts
echo "127.0.1.1	partybox" >> /etc/hosts

# ──────────────────────────────────────────────────────────────────────────────
# 11. Version record
# ──────────────────────────────────────────────────────────────────────────────
log "Recording version ${COMPANION_VERSION}"
mkdir -p /etc/partybox-companion
echo "${COMPANION_VERSION}" > /etc/partybox-companion/version

# ──────────────────────────────────────────────────────────────────────────────
# 12. Clean up
# ──────────────────────────────────────────────────────────────────────────────
log "Cleaning up"
apt-get clean
rm -rf /var/lib/apt/lists/*

log "Done: partybox-companion ${COMPANION_VERSION} installed"
log "  Venv:    ${INSTALL_PREFIX}"
log "  Binary:  /usr/local/bin/partybox-companion"
log "  Service: companion.service (enabled, starts on next boot)"
log "  Portal:  http://partybox.local (after boot)"
