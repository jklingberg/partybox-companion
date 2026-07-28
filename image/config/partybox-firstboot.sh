#!/bin/bash
# Generates a random per-device password for the 'pi' account's *local
# console* login (UART serial / physical HDMI+keyboard), on the device's
# real first boot -- never at image-build time, so every flashed device
# gets its own password rather than every device from one image build
# sharing whatever got baked in. See ADR-043.
#
# Completely independent of SSH: SSH ships disabled by default and, once
# enabled via the Portal, is always key-only -- this password is never
# usable over SSH. It exists purely so the UART physical-console recovery
# path (ADR-020) still has something to log in with.
set -euo pipefail

MARKER=/var/lib/companion/.firstboot-done
mkdir -p /var/lib/companion

if [ -f "${MARKER}" ]; then
    exit 0
fi

# set +o pipefail around this one line only: /dev/urandom feeds tr faster
# than `head -c 16` consumes, so once head has its 16 bytes and exits, tr's
# next write() gets SIGPIPE and dies with exit 141 -- which pipefail would
# otherwise propagate as this line's exit status, aborting the whole script
# under set -e *before PASSWORD is ever assigned*. That's not hypothetical:
# it reproduces on every single invocation. head still only ever reads (and
# this still only ever captures) exactly 16 bytes of real /dev/urandom
# output regardless of how tr's own process happens to exit.
set +o pipefail
PASSWORD="$(tr -dc 'A-HJ-NP-Za-km-z2-9' < /dev/urandom | head -c 16)"
set -o pipefail
echo "pi:${PASSWORD}" | chpasswd

# Prepended (not appended) so it's the first thing visible above whatever
# distro-default /etc/issue content follows. getty/serial-getty re-read this
# file fresh on every login prompt, so it stays visible on every subsequent
# boot too, not just this one -- it goes stale only if the password is later
# changed locally (see ADR-043's stated caveat).
{
    printf 'PartyBox Companion -- local console password for "pi": %s\n' "${PASSWORD}"
    printf '(Generated once on first boot; NOT the SSH password -- SSH ships\n'
    printf 'disabled by default and, once enabled via the Portal, is key-only.)\n\n'
    cat /etc/issue 2>/dev/null || true
} > /etc/issue.new
mv /etc/issue.new /etc/issue

touch "${MARKER}"
