#!/usr/bin/env python3
"""Stream audio over A2DP for an extended session while probing BLE control.

This is the core M3 evidence run. It streams local audio to the PartyBox for a
configurable duration (default 30 min) and, on a fixed interval, exercises the
BLE control channel and samples PipeWire's xrun counter. The summary reports
control success rate, control latency, xrun growth, and any disconnects — the
data needed to judge whether audio + control coexist over a long session.

    python audio_stream.py --duration 1800
    python audio_stream.py --duration 300 --source file:/home/jonathan/Music/x.wav
    python audio_stream.py --duration 600 --power-cycle-test   # toggle power once

Run on the Pi. Ctrl-C stops early and still writes a summary.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import statistics
from pathlib import Path

from m3lib import audio, proc
from m3lib.control import BleControl
from m3lib.evidence import Recorder
from m3lib.session import bring_up_a2dp, resolve_audio_mac

TONE_PATH = Path(__file__).resolve().parent / "evidence" / "_assets" / "tone-5min.wav"


async def _monitor(
    *,
    mac: str,
    node_name: str,
    ble: BleControl | None,
    interval: float,
    duration: float,
    rec: Recorder,
    stats: dict[str, object],
) -> None:
    """Probe control + sample xruns every ``interval`` seconds for ``duration``."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + duration
    latencies: list[float] = []
    control_ok = control_fail = 0
    baseline_xruns: int | None = None
    disconnects = 0

    while loop.time() < deadline:
        await asyncio.sleep(interval)
        a2dp_up = await audio.find_sink(mac) is not None
        if not a2dp_up:
            disconnects += 1

        xruns = await audio.xrun_count(node_name)
        if baseline_xruns is None and xruns is not None:
            baseline_xruns = xruns

        probe = None
        probe_err = None
        if ble is not None:
            result = await ble.probe()
            probe = result.ok
            probe_err = result.error
            if result.ok and result.latency_ms is not None:
                control_ok += 1
                latencies.append(result.latency_ms)
            else:
                control_fail += 1

        rec.event(
            "monitor_tick",
            a2dp=a2dp_up,
            ble=ble.connected if ble else None,
            ble_probe_ok=probe,
            ble_probe_err=probe_err,
            xruns=xruns,
            xrun_delta=(xruns - baseline_xruns)
            if (xruns is not None and baseline_xruns is not None)
            else None,
        )

    stats["control_ok"] = control_ok
    stats["control_fail"] = control_fail
    stats["control_latency_ms_p50"] = round(statistics.median(latencies), 1) if latencies else None
    stats["control_latency_ms_max"] = max(latencies) if latencies else None
    stats["xrun_growth"] = None
    final = await audio.xrun_count(node_name)
    if final is not None and baseline_xruns is not None:
        stats["xrun_growth"] = final - baseline_xruns
    stats["a2dp_disconnects"] = disconnects


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio-mac", help="BR/EDR address (else discover by name)")
    parser.add_argument("--duration", type=float, default=1800.0, help="session length, seconds")
    parser.add_argument("--source", default="tone", help="'tone' or 'file:/path/to/audio'")
    parser.add_argument(
        "--control-interval", type=float, default=5.0, help="seconds between probes"
    )
    parser.add_argument("--no-ble", action="store_true", help="stream only; skip BLE probing")
    parser.add_argument(
        "--power-cycle-test",
        action="store_true",
        help="once at mid-session, send power-off then power-on (interrupts audio)",
    )
    args = parser.parse_args()

    missing = proc.require("bluetoothctl", "pw-dump", "pw-play", "pw-top")
    if "tone" in args.source:
        missing += proc.require("ffmpeg")
    if missing:
        print(f"Missing required tools: {', '.join(missing)}")
        return 2

    if args.source == "tone":
        source = await audio.ensure_tone(TONE_PATH)
    elif args.source.startswith("file:"):
        source = Path(args.source[len("file:") :]).expanduser()
        if not source.exists():
            print(f"Source file not found: {source}")
            return 2
    else:
        print("--source must be 'tone' or 'file:/path'")
        return 2

    ble: BleControl | None = None
    with Recorder("audio_stream") as rec:
        await rec.capture_environment()
        rec.event(
            "config", duration=args.duration, source=str(source), interval=args.control_interval
        )

        mac = await resolve_audio_mac(args.audio_mac, rec)
        if mac is None:
            rec.summary("FAIL", reason="no PartyBox discovered")
            return 1
        node = await bring_up_a2dp(mac, rec)
        if node is None:
            rec.summary("FAIL", reason="A2DP did not reach a ready sink", audio_mac=mac)
            return 1

        if not args.no_ble:
            ble = BleControl()
            rec.event("ble_connect_attempt")
            ble_ok = await ble.connect()
            rec.event("ble_connected" if ble_ok else "ble_connect_failed")
            if not ble_ok:
                ble = None

        stats: dict[str, object] = {}
        play_task = asyncio.create_task(
            audio.play_loop(node, source, duration=args.duration, recorder=rec)
        )
        monitor_task = asyncio.create_task(
            _monitor(
                mac=mac,
                node_name=node.name,
                ble=ble,
                interval=args.control_interval,
                duration=args.duration,
                rec=rec,
                stats=stats,
            )
        )

        power_task: asyncio.Task[None] | None = None
        if args.power_cycle_test and ble is not None:
            power_task = asyncio.create_task(_deferred_power_cycle(ble, args.duration / 2, rec))

        interrupted = False
        try:
            await monitor_task
        except (KeyboardInterrupt, asyncio.CancelledError):
            interrupted = True
            rec.event("interrupted")
        finally:
            for task in (play_task, power_task):
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            if ble is not None:
                stats["ble_dropped"] = ble.dropped
                stats["ble_notifications"] = ble.notifications
                await ble.close()

        verdict = _verdict(stats, ble_used=ble is not None, interrupted=interrupted)
        rec.summary(verdict, audio_mac=mac, codec=node.codec, interrupted=interrupted, **stats)
        return 0 if verdict == "PASS" else 1


async def _deferred_power_cycle(ble: BleControl, delay: float, rec: Recorder) -> None:
    await asyncio.sleep(delay)
    rec.event("power_cycle_start")
    await ble.power_cycle()
    rec.event("power_cycle_done")


def _verdict(stats: dict[str, object], *, ble_used: bool, interrupted: bool) -> str:
    disconnects = stats.get("a2dp_disconnects") or 0
    control_fail = stats.get("control_fail") or 0
    if isinstance(disconnects, int) and disconnects > 0:
        return "FAIL"
    if ble_used and isinstance(control_fail, int) and control_fail > 0:
        return "DEGRADED"
    return "PASS"


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
