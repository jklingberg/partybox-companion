# Investigation Run — BLE control-plane wedge (2026-07-16 → 2026-07-17)

Not a release validation run: this documents the debugging of a field defect
("Turn off button broken while everything says connected") and the controlled
experiments that ruled candidate root causes in and out. Referenced by
[ADR-039](../../adr/039-ble-controller-wedge-self-heal.md).

## Environment

| Item | Value |
|---|---|
| Image | `0.1.0rc14` + `feat/audio-focus-detection` deployed via rsync |
| Host | Pi 5 at 192.168.1.221, kernel 6.6, BlueZ 5.66, Python 3.14.6 |
| Speaker | JBL PartyBox 520 (BR/EDR `50:1B:6A:14:FD:1D`; BLE RPA, rotates) |

## Defect as reported

Portal "Turn off" button failed every time. `GET /api/v1/health` claimed
`ble_connected: true`, `speaker_state: "on"`; `POST /api/v1/power/off`
returned **503 `speaker_disconnected` in 9 ms**. State persisted 8+ hours
(2026-07-16 22:37 → 2026-07-17 07:17) across a night where the speaker was
off — the manager never logged a single reconnect attempt.

## Root cause (confirmed by live task-stack dump)

`python3.14 -m asyncio pstree <pid>` against the running process showed the
`device-manager` task frozen at:

```
DeviceManager._poll_liveness
  → BatteryCapability.status
    → BleakTransport.write            # no timeout at this layer
      → BleakClientBlueZDBus.write_gatt_char
        → dbus_fast MessageBus.call   # reply never arrives; waits forever
```

The BLE link died with the liveness probe's GATT write in flight. bluetoothd
never delivered the D-Bus method reply (the device object was removed —
`bluetoothctl info <RPA>` said "not available"), and `MessageBus.call` has no
timeout. Consequences:

- The maintain loop froze inside `_poll_liveness`; the drain/disconnect
  detector was cancelled at that instant (single-notification-consumer
  design), so no code path ever observed the drop.
- Snapshot frozen at `connected/on/battery 92%` — the health endpoint lied.
- Every power command reached bleak's client, which *did* know it was
  disconnected → instant `BleakError("Not connected")` → 503.

**Fix:** `_GATT_IO_TIMEOUT = 10.0` on `BleakTransport.write/read/start_notify`
(`asyncio.wait_for`; `TimeoutError` is already in `_TRANSPORT_ERRORS`, so a
timed-out call surfaces as `ConnectionLostError` and the manager reconnects).
Deployed to hardware and verified: power off → 204 → standby; power on →
204 → on.

## Secondary defect: 25-minute connect outage (2026-07-17 morning)

After a service restart un-wedged the frozen loop, the manager logged **22
consecutive failed connect cycles over ~25 minutes** (06:49–07:14). Signature:
LE scanning kept finding the speaker's RPA, but every GATT connect failed
("could not connect to <RPA>"), and A2DP retries failed all night with
`br-connection-unknown`. `systemctl restart bluetooth` did **not** clear it.
The `companion.service` restart (whose `ExecStartPre` resets the adapter,
ADR-023) cleared it instantly — zero failures for the rest of the day.

## Experiments: what does NOT destabilize the GATT link

Hypotheses tested with btmon HCI captures on 2026-07-17, speaker on,
audio-focus FDDF discovery scans (12 s window / ~72 s period) running
throughout:

| # | Scenario | Duration | Discovery windows | Link drops / HCI errors |
|---|---|---|---|---|
| A | Idle (no audio) | 3 min | 9 enable events | **0** |
| B | Pi streaming A2DP (silent pcm via pw-play) | 4 min | 26 enable events | **0** |
| C | Phone connected multipoint, playing music | 5 min | (scans running) | **0** |

All three earlier suspects — the 60-second FDDF discovery cadence, concurrent
A2DP streaming load, and phone multipoint — produced **zero** disconnects,
supervision timeouts, or aborts on a healthy controller. During experiment C
the audio-focus classifier flipped `exclusive → contested`
(`source_count=0x06`, `connection_bits=0x09`) within one scan cycle, live-
validating the feature under test.

## Conclusion

Every 2026-07-16/17 symptom — flaky connects right after the evening boot,
the two drops ~61 s after connect, the mid-write link death that exposed the
hang, the overnight `br-connection-unknown` A2DP failures, and the morning
connect outage — is explained by a **single degraded controller state**
entered around the 2026-07-16 22:12 boot and cleared by the 07:17 adapter
reset. This is the known BCM4345 wedge class (ADR-023, ADR-028). The FDDF
scanning and multipoint features were bystanders.

Two systemic gaps addressed as a result:

1. **Unbounded GATT I/O** could freeze the manager forever → SDK-level
   `_GATT_IO_TIMEOUT` (this PR).
2. **No in-flight recovery** for the wedge: `ExecStartPre` only helps at
   service start, so a wedge entered mid-flight persisted until a human
   restarted something → DeviceManager wedge detection + adapter power-cycle
   self-heal ([ADR-039](../../adr/039-ble-controller-wedge-self-heal.md),
   this PR).
