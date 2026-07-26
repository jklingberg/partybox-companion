#!/bin/bash
# Applies the SSH access state SshAccessService (companion, unprivileged)
# writes to /var/lib/companion/{ssh_enabled,ssh_authorized_key} -- see
# ADR-042. Runs as root via companion-ssh-apply.service, triggered by
# companion over D-Bus (systemd1 Manager.StartUnit), authorized by a polkit
# rule scoped to exactly this unit name (installed by install.sh).
#
# Deliberately does not interpret ssh_authorized_key's contents as anything
# other than raw bytes copied verbatim into authorized_keys --
# SshAccessService has already validated every line before writing it; this
# script trusts that and never sources or evaluates the file's contents.
set -euo pipefail

STATE_DIR=/var/lib/companion
ENABLED_FILE="${STATE_DIR}/ssh_enabled"
KEY_FILE="${STATE_DIR}/ssh_authorized_key"
STATUS_FILE="${STATE_DIR}/ssh_status.json"

PI_SSH_DIR=/home/pi/.ssh
AUTHORIZED_KEYS="${PI_SSH_DIR}/authorized_keys"

enabled="false"
has_key="false"
error=""

# Writes ssh_status.json from whatever state has been established so far,
# via a temp-file + rename so companion (polling GET /api/v1/ssh/status)
# never observes a half-written file. Registered as an EXIT trap below so a
# failure partway through (a failed `install`, a failed `systemctl
# enable/disable`, ...) still leaves an accurate, non-stale status behind
# instead of silently keeping whatever the previous run last wrote.
write_status() {
    local applied_at tmp error_json
    applied_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    error_json="null"
    [ -n "${error}" ] && error_json="\"${error}\""
    tmp="${STATUS_FILE}.tmp"
    cat > "${tmp}" << EOF
{"enabled": ${enabled}, "has_key": ${has_key}, "applied_at": "${applied_at}", "error": ${error_json}}
EOF
    chown companion:companion "${tmp}"
    chmod 644 "${tmp}"
    mv -f "${tmp}" "${STATUS_FILE}"
}

on_exit() {
    local rc=$?
    if [ "${rc}" -ne 0 ] && [ -z "${error}" ]; then
        # A command failed under set -e before we got a chance to record a
        # more specific error message -- report the fact that apply failed
        # rather than leaving the Portal looking at a stale, possibly
        # contradictory, last-known-good status.
        error="apply failed (exit ${rc})"
        enabled="false"
    fi
    write_status
}
trap on_exit EXIT

if [ -f "${ENABLED_FILE}" ]; then
    read -r enabled_raw < "${ENABLED_FILE}" || enabled_raw=""
    [ "${enabled_raw}" = "true" ] && enabled="true"
fi

mkdir -p "${PI_SSH_DIR}"
chmod 700 "${PI_SSH_DIR}"
chown pi:pi "${PI_SSH_DIR}"

if [ -f "${KEY_FILE}" ] && [ -s "${KEY_FILE}" ]; then
    install -m 600 -o pi -g pi "${KEY_FILE}" "${AUTHORIZED_KEYS}"
    has_key="true"
else
    rm -f "${AUTHORIZED_KEYS}"
fi

if [ "${enabled}" = "true" ] && [ "${has_key}" = "false" ]; then
    # SshAccessService already refuses to request "enabled" with no key
    # configured -- this is belt-and-suspenders against the key file being
    # cleared or corrupted between that check and this unit actually running.
    enabled="false"
    error="no public key configured"
fi

if [ "${enabled}" = "true" ]; then
    systemctl enable --now ssh.service
else
    systemctl disable --now ssh.service
fi

# write_status runs via the EXIT trap above, whether we reach here or exit
# early due to a failed command.
