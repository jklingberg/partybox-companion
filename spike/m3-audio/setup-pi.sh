#!/usr/bin/env bash
# Sync the M3 spike to the Pi and build a venv with the partybox SDK.
#
# Run from the devcontainer (where Bluetooth itself does not work — see the
# memory note "bluetooth-runs-on-pi-not-devcontainer"). All actual validation
# runs happen on the Pi.
#
#   ./setup-pi.sh                 # uses jonathan@partybox, ~/m3-audio
#   PI=jonathan@partybox DEST=~/m3-audio ./setup-pi.sh
set -euo pipefail

PI="${PI:-jonathan@partybox}"
DEST="${DEST:-m3-audio}"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

echo ">> Syncing SDK + spike to ${PI}:${DEST}/"
ssh "${PI}" "mkdir -p ${DEST}"
# The spike imports the partybox SDK, so ship the SDK package alongside it.
rsync -a --delete \
  --exclude '__pycache__' --exclude '.mypy_cache' --exclude '.pytest_cache' \
  --exclude 'evidence/2*' --exclude 'evidence/_assets' --exclude 'venv' \
  "${REPO_ROOT}/spike/m3-audio/" "${PI}:${DEST}/run/"
rsync -a --delete --exclude '__pycache__' \
  "${REPO_ROOT}/packages/partybox/" "${PI}:${DEST}/partybox-sdk/"

echo ">> Building venv on the Pi"
ssh "${PI}" "bash -s" <<EOF
set -euo pipefail
cd "${DEST}"
python3 -m venv venv
./venv/bin/pip install --quiet --upgrade pip
# bleak (with dbus-fast on Linux) is the SDK's only runtime dependency.
./venv/bin/pip install --quiet bleak
# Make the SDK importable without building it: its pyproject license path
# assumes the monorepo layout, which doesn't survive a standalone sync. A .pth
# is all a spike needs — we only import partybox, never package it.
echo "\$(pwd)/partybox-sdk/src" > "\$(./venv/bin/python -c 'import site; print(site.getsitepackages()[0])')/partybox-sdk.pth"
./venv/bin/python -c "import partybox, bleak; print('SDK import OK')"
EOF

cat <<EOF

Done. Run validations on the Pi, e.g.:

  ssh ${PI}
  cd ${DEST}/run
  ../venv/bin/python audio_connect.py
  ../venv/bin/python audio_stream.py --duration 1800
  ../venv/bin/python reconnect_stress.py --cycles 10

Evidence is written under ${DEST}/run/evidence/<timestamp>-<run>/.
Pull it back with:
  rsync -a ${PI}:${DEST}/run/evidence/ ./spike/m3-audio/evidence/
EOF
