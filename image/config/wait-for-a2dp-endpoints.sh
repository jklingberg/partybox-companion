#!/bin/bash
# Poll BlueZ for A2DP Sink endpoint registration after a PipeWire/WirePlumber
# restart, replacing a fixed sleep in companion.service's ExecStartPre.
#
# This only covers the boot-time registration race, where WirePlumber's
# media endpoints appear asynchronously after it starts. It does not detect
# or fix runtime A2DP transport flapping — see ADR-028 "WirePlumber endpoint
# degradation investigation" for why those are distinct failure modes.
#
# Always exits 0: if the endpoint never registers within the timeout, service
# startup proceeds anyway. AudioService's own retry loop keeps retrying
# ConnectProfile and self-heals once BlueZ catches up — failing service
# startup here would be worse than a delayed first connect.
set -uo pipefail

readonly TIMEOUT_SECONDS="${1:-15}"
readonly A2DP_SINK_UUID="0000110b-0000-1000-8000-00805f9b34fb"

for ((i = 0; i < TIMEOUT_SECONDS; i++)); do
    if busctl get-property org.bluez /org/bluez/hci0 org.bluez.Adapter1 UUIDs 2>/dev/null \
        | grep -qi "$A2DP_SINK_UUID"; then
        exit 0
    fi
    sleep 1
done

exit 0
