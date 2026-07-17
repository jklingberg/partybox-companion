# ADR-039 — Runtime Self-Heal of a Wedged Bluetooth Controller

**Status:** Accepted
**Date:** 2026-07-17

---

## Context

ADR-023 resets the HCI controller at service start (`ExecStartPre`) and
explicitly deferred the harder case: a controller wedge that develops
**while the appliance is running**. That case materialized 2026-07-16/17
(see [the investigation run](../validation/runs/2026-07-17-ble-wedge-investigation.md)):
the BCM4345 entered a degraded state around boot and stayed in it for ~9
hours. Symptoms while wedged:

- LE scanning works — the speaker's RPA is found every cycle.
- Every GATT connect fails (`could not connect to <RPA>`); 22 consecutive
  failures over 25 minutes were observed.
- Classic-side A2DP connects fail with `br-connection-unknown`.
- `systemctl restart bluetooth` does **not** clear it; an adapter
  power-cycle (or the ExecStartPre reset via a service restart) clears it
  instantly.

Controlled experiments on a healthy controller (same day, btmon-verified)
showed zero link drops under idle, streaming, and phone-multipoint load with
FDDF discovery scans running — so the wedge is a controller state, not an
interaction bug in our services. It is sporadic and cannot be provoked on
demand; the system must therefore heal it, not avoid it.

The companion process cannot use ADR-023's tool at runtime: `hciconfig`
needs `CAP_NET_ADMIN`, which the hardened `companion` user does not hold.
But bluetoothd (running as root) exposes the equivalent through D-Bus:
setting `org.bluez.Adapter1.Powered` false → true, permitted to any bus
client under the default BlueZ D-Bus policy — the same access path the
process already uses for GATT, pairing, and A2DP.

## Decision

Two pieces, split along the existing layer boundary:

1. **Detection in `DeviceManager` (partyboxd), recovery injected.**
   The manager counts *dense* scan-found-it-but-connect-failed cycles — the
   wedge signature. Scan-empty cycles (speaker off) leave the counter
   untouched; a successful connect resets it; failures more than
   `_WEDGE_WINDOW` (600 s) apart restart the count. At
   `_WEDGE_CONNECT_FAILURES` (3) dense failures the manager calls an
   injected `adapter_recover_fn: Callable[[], Awaitable[bool]] | None`,
   then resumes its normal retry loop regardless of the outcome.
   Standalone partyboxd passes `None` and behaves exactly as before —
   partyboxd gains no BlueZ/D-Bus knowledge.

   Three guards keep the trigger honest:

   - **ADR-034 grace:** connect failures within `_POWER_COMMAND_GRACE`
     (60 s) of a power command are not counted — the speaker resets its own
     BLE stack for ~15-17 s after every power on/off, and dense failures are
     the *expected* shape there, not wedge evidence.
   - **Cool-down:** recoveries are rate-limited by `_RECOVERY_COOLDOWN`
     (900 s). A power-cycle drops every connection on the adapter, including
     possibly healthy A2DP audio; a failure mode recovery does not fix must
     not become an adapter cycle every few minutes. Counters keep their
     value while suppressed, so the first failure after the cool-down
     re-triggers immediately.
   - **Scan-error trigger:** `Scanner.find` *raising* (as opposed to a clean
     empty scan) means the adapter itself is unusable — most importantly the
     powered-off state a half-completed recovery could leave behind, which
     the connect-failure counter can never observe because no connect is
     ever attempted. `_WEDGE_SCAN_ERRORS` (5) consecutive scan errors also
     request recovery, whose `Powered=true` brings the adapter back.

2. **Recovery in companion: D-Bus adapter power-cycle, subprocess-isolated.**
   `companion.services.adapter_recovery.reset_adapter()` runs
   `companion.services._adapter_reset` in a subprocess (the established
   bleak/dbus-fast isolation pattern, cf. `_a2dp_connect`, ADR-027's
   helpers): `Powered=false`, 1 s settle, `Powered=true`. The wrapper never
   raises; spawn failure, helper `err:` output, and timeout (20 s, killed)
   all collapse to `False`.

   The helper treats *exiting with the adapter powered off* as the worst
   possible outcome and defends against it three ways: the power-on runs in
   a `finally` with one retry (covers errors and cancellation after the
   power-off); SIGTERM cancels the task instead of killing the process, so
   that `finally` also runs when systemd stops the service mid-recovery;
   and each `Powered` write is individually bounded (5 s) so a lost D-Bus
   reply surfaces as an error while the parent is still listening rather
   than stranding the adapter via the parent's kill-timeout. For the same
   reason, the wrapper deliberately does **not** kill the helper when
   cancelled — an orphaned helper finishes the power-cycle on its own.

The power-cycle intentionally drops every active connection on the adapter
(BLE control + A2DP). By the time recovery fires, the control plane has
already been down for at least three failed cycles and the wedge breaks the
Classic side too; AudioService's existing retry loop re-establishes A2DP
afterwards.

## Alternatives considered

### `sudo hciconfig hci0 reset` from the service

Requires a sudoers grant the hardening model forbids (`NoNewPrivileges`
blocks sudo outright — see ADR-019) or `CAP_NET_ADMIN`, rejected in ADR-023
for being far broader than the need.

### Ask systemd to restart the whole companion service

A `Restart=` escalation (supervisor exits, ExecStartPre resets the adapter)
would work but throws away all process state — Portal sessions, WS clients,
Spotify session — to fix a Bluetooth-local problem, and turns a 3-second
recovery into a full restart. Kept as the operator's manual fallback.

### Restart bluetoothd instead

Observed ineffective against this wedge class (2026-07-17: `systemctl
restart bluetooth` did not clear it; the adapter power-cycle did).

## Consequences

- A wedge that previously required human intervention now heals in-flight;
  worst case added latency is three failed connect cycles plus ~3 s of
  power-cycle before attempts resume against a healthy controller.
- A speaker that is genuinely on but repeatedly refusing connections for
  reasons other than a wedge triggers at most one power-cycle per
  `_RECOVERY_COOLDOWN` (15 min) — the cost of a false positive is bounded
  to one brief A2DP interruption per cool-down period.
- Because the maintain loop now also disconnects its device on every exit
  (a false-positive I/O timeout could otherwise orphan BlueZ's single live
  connection to the speaker), recovery and reconnect always start from a
  released connection.
- The recovery path is untestable in CI beyond its failure contract (no
  system bus); its success path is hardware-verified (2026-07-17, run as
  the `companion` user on the appliance).
