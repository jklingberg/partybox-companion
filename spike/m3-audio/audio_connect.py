#!/usr/bin/env python3
"""Bring up A2DP audio and BLE control together, then report the result.

The smallest possible vertical slice of the M3 question: can the Pi hold an
A2DP audio connection to the PartyBox *and* a BLE control connection at the same
time? No music yet — this validates that the two links coexist at connect time
and reports the negotiated A2DP codec.

    python audio_connect.py                 # discover the speaker by name
    python audio_connect.py --audio-mac 50:1B:6A:14:FD:1D
    python audio_connect.py --no-ble        # A2DP only (isolate the audio path)

Run on the Pi. See README.md for setup.
"""

from __future__ import annotations

import argparse
import asyncio

from m3lib import audio, proc
from m3lib.control import BleControl
from m3lib.evidence import Recorder
from m3lib.session import bring_up_a2dp, resolve_audio_mac


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio-mac", help="BR/EDR address of the speaker (else discover by name)")
    parser.add_argument("--no-ble", action="store_true", help="skip the BLE control connection")
    parser.add_argument("--hold", type=float, default=10.0, help="seconds to hold both links up")
    args = parser.parse_args()

    missing = proc.require("bluetoothctl", "pw-dump")
    if missing:
        print(f"Missing required tools: {', '.join(missing)}")
        return 2

    ble: BleControl | None = None
    with Recorder("audio_connect") as rec:
        await rec.capture_environment()

        mac = await resolve_audio_mac(args.audio_mac, rec)
        if mac is None:
            rec.summary("FAIL", reason="no PartyBox discovered")
            return 1

        node = await bring_up_a2dp(mac, rec)
        if node is None:
            rec.summary("FAIL", reason="A2DP did not reach a ready sink", audio_mac=mac)
            return 1

        control_ok = None
        if not args.no_ble:
            ble = BleControl()
            rec.event("ble_connect_attempt")
            if await ble.connect():
                rec.event("ble_connected")
                result = await ble.probe()
                control_ok = result.ok
                rec.event(
                    "ble_probe", ok=result.ok, latency_ms=result.latency_ms, error=result.error
                )
            else:
                control_ok = False
                rec.event("ble_connect_failed")

        for _ in range(int(args.hold)):
            await asyncio.sleep(1.0)
            info = await audio.find_sink(mac)
            rec.event(
                "hold_tick",
                a2dp=info is not None,
                ble=ble.connected if ble else None,
                ble_dropped=ble.dropped if ble else None,
            )

        coexists = node is not None and (args.no_ble or control_ok is True)
        rec.summary(
            "PASS" if coexists else "FAIL",
            audio_mac=mac,
            codec=node.codec,
            profile=node.profile,
            ble_control_ok=control_ok,
            ble_notifications=ble.notifications if ble else None,
        )
        if ble is not None:
            await ble.close()
        return 0 if coexists else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
