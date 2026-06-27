#!/usr/bin/env python3
"""Diagnostic: LE-bond the BLE control identity and test a write.

M3 surfaced that bonding the A2DP (BR/EDR) address is *not* enough: the BLE
control link is a separate LE identity that advertises with rotating private
addresses. Unbonded, connects to it race the address rotation (timeouts) and
the link is fragile under A2DP load. BlueZ can only resolve a rotating RPA to a
stable identity once it holds the device's IRK — i.e. once the LE link is bonded.

This tool tests that hypothesis: find the PartyBox by its **control service**
(so we get the LE/GATT endpoint, not the BR/EDR audio one), connect, pair/bond,
report the resolved identity address, and write the (idempotent) power-on frame
to confirm control works on the bonded link.

    python diag_ble_bond.py        # speaker must be awake + in pairing mode

Run on the Pi. Bonding a new device requires the speaker in pairing mode.
"""

from __future__ import annotations

import asyncio

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from m3lib.control import POWER_ON
from m3lib.evidence import Recorder
from partybox.bluetooth import CONTROL_SERVICE_UUID, TX_CHAR_UUID


async def _strongest_partybox(rec: Recorder) -> BLEDevice | None:
    """Scan and return the strongest-signal PartyBox LE device handle."""
    found = await BleakScanner.discover(timeout=10.0, return_adv=True)
    candidates: list[tuple[float, BLEDevice]] = []
    for device, adv in found.values():
        name = adv.local_name or device.name or ""
        if CONTROL_SERVICE_UUID in (adv.service_uuids or []) or "PartyBox" in name:
            rec.event("candidate", address=device.address, name=name, rssi=adv.rssi)
            candidates.append((adv.rssi if adv.rssi is not None else -999, device))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]


async def main() -> int:
    with Recorder("diag_ble_bond") as rec:
        await rec.capture_environment()

        # Rotating RPAs go stale fast, so rescan fresh on each attempt and always
        # take the strongest signal (a far/weak RPA is the likeliest to time out).
        last_error = "no PartyBox seen"
        for attempt in range(1, 4):
            rec.event("scan_start", attempt=attempt, service=CONTROL_SERVICE_UUID)
            target = await _strongest_partybox(rec)
            if target is None:
                last_error = "no PartyBox LE endpoint advertised"
                continue

            address = target.address
            client = BleakClient(target)
            try:
                await client.connect()
                rec.event("connected", attempt=attempt, address=address, up=client.is_connected)
                await client.pair()
                rec.event("paired", attempt=attempt)
                await client.write_gatt_char(TX_CHAR_UUID, POWER_ON, response=True)
                rec.event("control_write_ok", frame=POWER_ON.hex())
            except (BleakError, OSError, TimeoutError, EOFError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                rec.event("attempt_failed", attempt=attempt, address=address, error=last_error)
                with _suppress():
                    await client.disconnect()
                continue
            else:
                await client.disconnect()
                rec.summary(
                    "PASS",
                    attempt=attempt,
                    scan_address=address,
                    note="LE control link bonded; re-run audio_stream to test reconnect",
                )
                return 0

        rec.summary("FAIL", reason=last_error)
        return 1


class _suppress:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> bool:
        return exc_type is not None


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
