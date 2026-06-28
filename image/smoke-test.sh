#!/usr/bin/env bash
# Release smoke test for the installed Companion appliance.
#
# Called by the release workflow (release.yml) immediately after install.sh,
# while still inside the QEMU ARM64 chroot. A non-zero exit blocks publishing.
#
# Tests:
#   1. Required binaries and service artefacts are present
#   2. companion.service is enabled in systemd
#   3. Companion starts, the HTTP server binds, and GET /api/v1/health returns
#      {"status": "ok"} within 30 seconds
#
# The QEMU chroot has no Bluetooth adapter. DeviceManager handles this
# gracefully — _scan() catches the BleakError and retries on a 5-second loop,
# so it never crashes the process. Uvicorn starts independently and serves
# requests immediately. The health endpoint returns speaker_connected=false,
# which is expected and correct here.

set -euo pipefail

HEALTH_URL=http://localhost:8080/api/v1/health
DATA_DIR="/tmp/companion-smoke-$$"

log()  { printf '==> %s\n'        "$*" >&2; }
pass() { printf '    [PASS] %s\n' "$*" >&2; }
fail() { printf '    [FAIL] %s\n' "$*" >&2; exit 1; }

# ──────────────────────────────────────────────────────────────────────────────
# 1. Artifact verification
# ──────────────────────────────────────────────────────────────────────────────
log "Verifying installation artefacts"

test -x /usr/local/bin/partybox-companion \
    && pass "partybox-companion binary present" \
    || fail "partybox-companion binary missing at /usr/local/bin/partybox-companion"

test -x /usr/local/bin/librespot \
    && pass "librespot binary present" \
    || fail "librespot binary missing at /usr/local/bin/librespot"

test -f /lib/systemd/system/companion.service \
    && pass "companion.service unit installed" \
    || fail "companion.service missing at /lib/systemd/system/companion.service"

test -L /etc/systemd/system/multi-user.target.wants/companion.service \
    && pass "companion.service enabled" \
    || fail "companion.service not enabled (symlink missing in multi-user.target.wants)"

# ──────────────────────────────────────────────────────────────────────────────
# 2. Installed version
# ──────────────────────────────────────────────────────────────────────────────
log "Checking installed version"
INSTALLED=$(/opt/partybox-companion/bin/python - <<'PY'
from importlib.metadata import version, PackageNotFoundError
try:
    print(version("partybox-companion"))
except PackageNotFoundError:
    print("UNKNOWN")
PY
)
pass "partybox-companion==${INSTALLED}"

# ──────────────────────────────────────────────────────────────────────────────
# 3. Health endpoint
# ──────────────────────────────────────────────────────────────────────────────
log "Starting Companion"
mkdir -p "${DATA_DIR}"

# Run companion directly, not via systemd.
# COMPANION_DATA_DIR overrides the default (/var/lib/companion) so the process
# does not need the StateDirectory created by systemd at first boot.
COMPANION_DATA_DIR="${DATA_DIR}" /usr/local/bin/partybox-companion &
COMPANION_PID=$!
trap 'kill "${COMPANION_PID}" 2>/dev/null || true; rm -rf "${DATA_DIR}"' EXIT

HEALTH_JSON="${DATA_DIR}/health.json"

log "Polling ${HEALTH_URL} (30s timeout)"
for i in $(seq 1 30); do
    if curl -sf "${HEALTH_URL}" -o "${HEALTH_JSON}" 2>/dev/null; then
        break
    fi
    if [ "${i}" -eq 30 ]; then
        fail "health endpoint did not respond after 30 seconds"
    fi
    sleep 1
done

STATUS=$(/opt/partybox-companion/bin/python - <<PY
import json, sys
with open("${HEALTH_JSON}") as f:
    d = json.load(f)
print(d.get("status", ""))
PY
)

[ "${STATUS}" = "ok" ] \
    && pass "GET /api/v1/health → status=${STATUS}" \
    || fail "GET /api/v1/health returned unexpected status: ${STATUS}"

log "Smoke test passed"
