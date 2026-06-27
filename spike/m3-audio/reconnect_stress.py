#!/usr/bin/env python3
"""Repeatedly drop and re-establish the connections; measure recovery.

Answers the reconnection questions in the M3 brief: does the system reconnect
automatically, how long does it take, does it survive standby, and is bonding
required for reliable reconnect?

Each cycle: confirm A2DP (and optionally BLE) is up, play a short burst, drop
the link, settle, then attempt to reconnect and time how long recovery takes.

    python reconnect_stress.py --cycles 10                 # script-driven drops
    python reconnect_stress.py --cycles 5 --mode standby   # wait for speaker standby

In ``standby`` mode the script waits for the speaker to enter standby on its own
(or be put there) rather than issuing a disconnect — this is the harder, more
realistic reconnect path. Run on the Pi.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import statistics
from pathlib import Path

from m3lib import audio, bluez, proc
from m3lib.control import BleControl
from m3lib.evidence import Recorder
from m3lib.session import bring_up_a2dp, resolve_audio_mac

BURST_PATH = Path(__file__).resolve().parent / "evidence" / "_assets" / "tone-5min.wav"


async def _reconnect(mac: str, *, timeout: float) -> tuple[bool, float | None]:
    """Attempt A2DP reconnect; return (success, seconds_taken)."""
    loop = asyncio.get_event_loop()
    start = loop.time()
    await bluez.connect(mac)
    if not await bluez.wait_connected(mac, timeout=timeout):
        return False, None
    node = await audio.wait_for_sink(mac, timeout=timeout)
    if node is None:
        return False, None
    return True, round(loop.time() - start, 2)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio-mac", help="BR/EDR address (else discover by name)")
    parser.add_argument("--cycles", type=int, default=10, help="number of drop/reconnect cycles")
    parser.add_argument("--mode", choices=("disconnect", "standby"), default="disconnect")
    parser.add_argument("--settle", type=float, default=5.0, help="seconds to wait after dropping")
    parser.add_argument("--reconnect-timeout", type=float, default=30.0)
    parser.add_argument("--check-ble", action="store_true", help="also reconnect BLE each cycle")
    args = parser.parse_args()

    missing = proc.require("bluetoothctl", "pw-dump", "pw-play", "ffmpeg")
    if missing:
        print(f"Missing required tools: {', '.join(missing)}")
        return 2

    source = await audio.ensure_tone(BURST_PATH)

    with Recorder("reconnect_stress") as rec:
        await rec.capture_environment()
        rec.event("config", cycles=args.cycles, mode=args.mode, settle=args.settle)

        mac = await resolve_audio_mac(args.audio_mac, rec)
        if mac is None:
            rec.summary("FAIL", reason="no PartyBox discovered")
            return 1

        info = await bluez.info(mac)
        bonded = bool(info and info.bonded)
        rec.event("bonding_state", bonded=bonded, paired=bool(info and info.paired))

        node = await bring_up_a2dp(mac, rec)
        if node is None:
            rec.summary(
                "FAIL", reason="A2DP did not reach a ready sink", audio_mac=mac, bonded=bonded
            )
            return 1

        successes = 0
        times: list[float] = []
        for cycle in range(1, args.cycles + 1):
            rec.event("cycle_start", cycle=cycle)
            # Short audio burst to confirm the link actually carries audio.
            await _short_burst(node, source, rec)

            if args.mode == "disconnect":
                drop = await bluez.disconnect(mac)
                rec.event("dropped", method="disconnect", ok=drop.ok)
            else:
                rec.event("await_standby", hint="put the speaker into standby now")
                await bluez.wait_connected(mac, timeout=120.0, want=False)
                rec.event("dropped", method="standby")

            await asyncio.sleep(args.settle)
            ok, seconds = await _reconnect(mac, timeout=args.reconnect_timeout)
            rec.event("reconnect", cycle=cycle, ok=ok, seconds=seconds)
            if ok:
                successes += 1
                if seconds is not None:
                    times.append(seconds)
                node = await audio.find_sink(mac) or node

            if args.check_ble:
                ble = BleControl()
                ble_ok = await ble.connect()
                rec.event("ble_reconnect", cycle=cycle, ok=ble_ok)
                await ble.close()

        rate = successes / args.cycles if args.cycles else 0.0
        verdict = "PASS" if rate == 1.0 else ("DEGRADED" if rate >= 0.8 else "FAIL")
        rec.summary(
            verdict,
            audio_mac=mac,
            bonded=bonded,
            cycles=args.cycles,
            reconnect_success=successes,
            reconnect_rate=round(rate, 3),
            reconnect_seconds_median=round(statistics.median(times), 2) if times else None,
            reconnect_seconds_max=max(times) if times else None,
        )
        return 0 if verdict == "PASS" else 1


async def _short_burst(node: audio.SinkNode, source: Path, rec: Recorder) -> None:
    """Play a few seconds of audio to confirm the link carries sound."""
    task = asyncio.create_task(audio.play(node, source, recorder=rec))
    await asyncio.sleep(4.0)
    if not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    rec.event("burst_done")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
