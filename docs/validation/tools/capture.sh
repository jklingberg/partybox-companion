#!/usr/bin/env bash
# Evidence collector for the appliance validation suite.
#
# Grabs one correlated snapshot of appliance state — API, BlueZ, PipeWire,
# librespot, journal — into a timestamped directory. Run it immediately
# after a scenario's physical action so the journal window brackets the
# event; journald is volatile, so collect before any reboot.
#
#   ./capture.sh <label> [journal-since]
#
#   label          short slug for the snapshot dir, e.g. phys-06-phone-connect
#   journal-since  journalctl --since value (default "5 min ago")
#
# Env:
#   PI    ssh target (default pi@192.168.1.221)
#   KEY   ssh identity (default $CLAUDE_CONFIG_DIR/ssh/partybox_ed25519)
#   OUT   output root (default ./captures)
set -uo pipefail

LABEL="${1:?usage: capture.sh <label> [journal-since]}"
SINCE="${2:-5 min ago}"
PI="${PI:-pi@192.168.1.221}"
KEY="${KEY:-$CLAUDE_CONFIG_DIR/ssh/partybox_ed25519}"
OUT="${OUT:-./captures}"

SPEAKER_BREDR="${SPEAKER_BREDR:-50:1B:6A:14:FD:1D}"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DIR="$OUT/${STAMP}-${LABEL}"
mkdir -p "$DIR"

SSH=(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 -i "$KEY" "$PI")

run() { # run <outfile> <remote command>
    local f="$1"; shift
    { echo "\$ $*"; "${SSH[@]}" "$*" 2>&1; } > "$DIR/$f"
}

echo "capture -> $DIR"

# --- API surface: what the product claims about itself -------------------
run api-health.txt   "curl -s --max-time 5 http://localhost/api/v1/health"
run api-speaker.txt  "curl -s --max-time 5 http://localhost/api/v1/speaker"
run api-audio.txt    "curl -s --max-time 5 http://localhost/api/v1/audio"
run api-spotify.txt  "curl -s --max-time 5 http://localhost/api/v1/spotify"

# --- Bluetooth: both links, plus who else the adapter knows about --------
run bt-controller.txt "bluetoothctl show"
run bt-speaker.txt    "bluetoothctl info $SPEAKER_BREDR"
run bt-devices.txt    "bluetoothctl devices; echo '--- connected ---'; bluetoothctl devices Connected"

# --- Audio graph: is there a sink, and is it glitching? ------------------
run pw-status.txt "XDG_RUNTIME_DIR=/run/user/1000 wpctl status"
# pw-top's ERR column is the xrun counter — the objective stutter measure.
run pw-top.txt    "XDG_RUNTIME_DIR=/run/user/1000 timeout 6 pw-top -b -n 3"

# --- Processes: librespot alive == Spotify Connect advertised ------------
run procs.txt "ps -eo pid,etime,rss,pcpu,args | grep -E 'librespot|companion|shairport' | grep -v grep"

# --- Journal: the causal chain ------------------------------------------
run journal.txt          "journalctl -u companion --since '$SINCE' --no-pager -o short-precise"
run journal-monotonic.txt "journalctl -u companion --since '$SINCE' --no-pager -o short-monotonic"
run journal-bluetooth.txt "journalctl -u bluetooth --since '$SINCE' --no-pager -o short-precise"

# --- Host -----------------------------------------------------------------
run host.txt "uptime; echo '--- ntp ---'; timedatectl | head -20; echo '--- failed units ---'; systemctl --failed --no-pager"

echo "--- health ---"
cat "$DIR/api-health.txt"
echo "--- signal grep ---"
grep -nE 'scan attempt|connection lost|connection failed|audio gate|audio focus|adapter recovery|a2dp|A2DP|audio_ready|librespot|WARN|ERROR' \
    "$DIR/journal.txt" | tail -40
