# 01 — Architecture Review

Reviewed: all of `packages/partybox`, `packages/partyboxd`, `packages/companion`,
`system/`, `image/`, ADR-001 … ADR-038. Date: 2026-07-11.

---

## Part A — Decisions I would keep, and why they are robust

These are endorsements, not filler. Each was checked against the code, not just
the ADR text.

### A1. The four-layer, one-way dependency stack (ADR-002/003/004/005)

`partybox → partyboxd → companion → clients` is genuinely enforced: `partybox`
imports only `bleak`; `partyboxd` never imports `companion`; the one place the
layers needed to cross *upward* (companion services fanning events into
partyboxd's WebSocket) was solved with a structural `Protocol`
(`partyboxd/api/ws.py::EventSource`) instead of an import. That is the correct
instinct executed correctly. The layering has already paid for itself twice:
the `EventBus` generalization (ADR-036) and the volume authority model
(ADR-022) both slotted in without violating a boundary.

### A2. Capability model with active probing (ADR-006/032)

For an undocumented protocol across an unknown model matrix, "probe the wire,
expose `None`" beats a model table in every dimension that matters:
correctness under firmware drift, zero maintenance, honest API. The
`_detect_battery` timeout-means-absent design (`device/partybox.py`) is the
right call and `redetect_battery()` closes the asleep-at-connect hole. Keep the
ADR-032 rule about introducing a `CapabilityDetector` before the third probe.

### A3. Evidence-only protocol discipline

`protocol/constants.py` states "speculative values are never added here" and
the codebase honors it — `model()`/`serial_number()`/`VolumeCapability` raise
`NotImplementedError` rather than shipping guesses. Combined with real-capture
byte fixtures in tests, this is the single strongest cultural asset the project
has. Protect it in review.

### A4. The signal → scene pipeline and its two rules (ADR-035)

Rule 1 (single ownership of every fact) and Rule 2 (command outcome ≠ transport
recovery) are the kind of rules most projects learn after years of state-sync
bugs. The Portal's `deriveScene()` precedence list makes UI state auditable.
One violation survives in the Portal — see UX-01 in
[06-ux-review.md](06-ux-review.md) — which proves the rule is load-bearing.

### A5. Two-tier recovery: Supervisor + systemd (ADR-024)

Correct division. The supervisor is small (286 lines), readable, and the
"services can't restart themselves" argument is airtight. Health tracking was
built before it was needed and paid off in ADR-037 with zero supervisor
changes, exactly as predicted.

### A6. Factory reset contract (ADR-031)

"Indistinguishable from a freshly-flashed image" is testable and the
delete-don't-write-defaults detail (`ConfigStore.reset()`) shows the contract
is actually understood. Most appliances never write this down.

### A7. Interoperability/legal positioning (ADR-012)

Clean-room discipline, gitignored `research/`, observation-not-transcription
documentation rules. This is what lets the project survive a HARMAN legal
glance. Non-negotiable to maintain.

### A8. Push-not-poll WS fan-in (ADR-036)

The merged-stream design, the subscribe-before-task-creation race fix, and the
documented backpressure/ordering semantics are better engineered than most
production WebSocket layers. The "generalize at the third instance" heuristic
(ADR-032) being *cited* when extracting `EventBus` is process working as
intended.

---

## Part B — Decisions I would change

### ARCH-01 — The subprocess-per-D-Bus-call workaround is a scar, not a fix
**Severity:** P1 · **Status:** OPEN · **Where:** `companion/services/_a2dp_connect.py`, `companion/services/audio.py::_run_subprocess`, ADR-028

Every A2DP health check (once per 60 s, plus every standby recheck, plus every
connect attempt) spawns a **full Python interpreter**, connects a fresh D-Bus
`MessageBus`, introspects BlueZ, and exits. On the Pi 3 B+ that is ~1 s of CPU
per check; on the *stated target hardware* (Pi Zero 2 W, README) it will be
several times worse and has never been measured there.

The deeper problem: the root cause was never actually diagnosed. ADR-028 says
running a second `MessageBus` in bleak's loop "risks interaction" and that the
conflict "is inherent to dbus-fast's asyncio integration and cannot be fixed by
configuration" — but no minimal reproduction exists, no upstream issue was
filed, and `PairingService` and `login1_dbus.power_off()` **do** run in-process
`MessageBus` instances in the same loop (rationalized as "distinct phases",
which is untrue for `power_off`, called while bleak is fully active, and for
`AudioService._disconnect()`, which uses in-process `BluezClient` on the hot
path after failed connects). Either the conflict is real — in which case three
in-process call sites are latent bugs — or it isn't, and the subprocess
machinery is unnecessary. Both can't be true.

**Recommendation:** (1) Build the minimal repro: two `MessageBus` instances +
one bleak client in one loop, hammer `GetManagedObjects`. (2) If real, file
upstream and replace per-call subprocesses with **one long-lived broker
subprocess** speaking a line protocol over stdin/stdout — same isolation, no
per-call interpreter startup, and it unlocks the `PropertiesChanged`
subscription ADR-028 defers (near-instant A2DP disconnect detection instead of
60 s polling). (3) If not real, delete `_a2dp_connect.py` entirely.

### ARCH-02 — The single-consumer BLE receive queue is the SDK's structural weakness
**Severity:** P1 · **Status:** OPEN · **Where:** `partybox/bluetooth/transport.py` (`receive()`), `partybox/device/partybox.py::drain_until_disconnect`, `partyboxd/device/manager.py::_drain_with_health_check`

The transport has exactly one notification consumer. Consequences ripple
upward: `DeviceManager` must run a cancel-the-drain / probe / recreate-the-
drain dance every 15 s (`_drain_with_health_check`), capabilities cannot be
used concurrently, every request/response helper re-implements its own
skip-unrelated-frames loop with its own deadline
(`DeviceInfoCapability.firmware_version`, `BatteryCapability.status`), and the
`FrameReassembler` exists per-call instead of per-connection — a fragmented
frame abandoned by one caller leaks its tail fragments to the next reader
(today: harmlessly discarded by the drain, but only by luck of scheduling).

**Recommendation:** Move to a **demultiplexer owned by the device**: one
permanent reader task feeds a per-connection `FrameReassembler`, decoded frames
route to (a) pending request futures keyed by expected response opcode and
(b) an events stream for unsolicited notifications. This one change deletes
the drain/cancel dance, makes capabilities concurrency-safe, gives the SDK its
long-promised `device.events()` (currently a TODO that *discards* button
presses and state pushes — the most valuable unexploited protocol asset), and
makes the daemon's liveness probe an ordinary request instead of choreography.
It is the prerequisite for real BLE volume (ADR-022 Phase 2) and for
standby-push detection replacing the 15 s probe loop.

### ARCH-03 — The audio path routes through the `pi` user's login session
**Severity:** P1 · **Status:** OPEN · **Where:** `system/systemd/companion.service` (`chmod 755 /run/user/1000`, `systemctl --user -M pi@ restart pipewire`), `image/install.sh`, ADR-019/028

A hardened system service (`companion`, nologin, `ProtectSystem=strict`)
depends at runtime on the **user session of an interactive login account with
a default password**: PipeWire runs in `pi`'s session, companion reaches the
socket only because an `ExecStartPre` chmods `pi`'s `XDG_RUNTIME_DIR` to 755,
and unit ordering pins `user@1000.service`. This is three kinds of wrong at
once: a privacy boundary is broken open at boot (any local process can now
reach `pi`'s runtime dir), the audio stack's lifecycle is coupled to a login
session that has no reason to exist on an appliance, and deleting/renaming the
`pi` user (which SEC-01 requires you to at least allow) breaks audio.

ADR-019 already laid the groundwork for the fix and it was never taken:
`RuntimeDirectory=companion` exists specifically so "enabling PipeWire for the
companion user requires no change to this unit."

**Recommendation:** Run PipeWire/WirePlumber as the `companion` user
(`loginctl enable-linger companion`, user units in
`/var/lib/companion/.config` or drop-ins) **or** as true system-wide services
(the `main-embedded` WirePlumber profile from ADR-028 is literally designed
for this). Then delete the chmod, the `-M pi@` restart, and the `user@1000`
ordering. This also unblocks removing the `pi` account entirely for users who
want SSH-key-only or no interactive access.

### ARCH-04 — "Unified volume" (M15/ADR-022) shipped as a facade with no actuator
**Severity:** P1 (because of INC-2) · **Status:** OPEN · **Where:** `companion/volume.py`, `companion/services/router.py` (volume endpoints), `partybox/device/capabilities/volume.py`

`POST /api/v1/volume` writes an in-memory integer that controls **nothing** —
BLE volume raises `NotImplementedError`, and nothing maps `VolumeState` onto
librespot softvol or the PipeWire node. Meanwhile the confirmed audio-UX
defect INC-2 (WirePlumber's 0.40 default node volume → music at 40 % vs. loud
native sounds, RC13 run report) is exactly a volume-actuation problem the
"unified volume model" claims to own and cannot touch.

The milestone description says "the backend switches transparently once BLE is
confirmed" — but a *usable* actuator exists today without any protocol work:
the PipeWire bluez node volume (`wpctl set-volume` / PipeWire native API),
which the RC13 punch list already requires pinning to 100 % on connect.

**Recommendation:** Make `AudioService` (or a small `VolumeActuator`) set the
PipeWire node volume: pin to 100 % on A2DP connect (fixes INC-2), then back
`POST /api/v1/volume` with the same mechanism so the API stops lying. Keep the
`source` field; add `"pipewire"`. BLE hardware volume remains the eventual
Phase 2. If that's not done for v1.0, **remove or 501 the volume endpoints** —
an API that accepts writes and silently does nothing is worse than no API
(clients like Home Assistant will build on it and file bugs).

### ARCH-05 — Hardcoded singletons: `hci0`, `/run/user/1000`, hostname `partybox`, `wlan0`
**Severity:** P2 · **Status:** OPEN · **Where:** `_a2dp_connect.py` (`_ADAPTER = "/org/bluez/hci0"`), `bluez_dbus.py` (`_ADAPTER_PATH`), `companion.service` (three `ExecStartPre` lines), `install.sh` (hostname)

Any Pi with a USB Bluetooth dongle (a *recommended* mitigation for the BCM4345
UART corruption documented in ADR-028!) enumerates it as `hci1` and the whole
appliance silently breaks. Two appliances on one network collide on
`partybox.local` (Avahi will rename one to `partybox-2.local`, but librespot's
"PartyBox" and the fixed `/etc/hostname` don't follow, and no Portal rename
exists — ADR-025 removed the appliance-name concept entirely on a
single-appliance-per-household assumption that a *party speaker* product will
violate). WiFi interface is configurable (`COMPANION_WIFI__INTERFACE`) but the
BT adapter is not.

**Recommendation:** (1) Adapter selection setting + auto-detect first powered
adapter via BlueZ ObjectManager; thread it through `_a2dp_connect`,
`bluez_dbus`, and the unit's `hciconfig` line. (2) First-boot hostname
uniquification (append last 2 MAC octets) *or* a Portal rename flow that
updates hostname + Avahi + librespot name together. ADR-019 already noted
hostname uniqueness as an open item; it never landed.

### ARCH-06 — In-process request/response has no seam for the one long-running request
**Severity:** P3 · **Status:** ACCEPTED-RISK

`POST /power/on` can now block ~20 s (ADR-034) inside a uvicorn worker.
Single-user appliance, fine — but note the pattern: as more "wait for
hardware" semantics accrete, the synchronous-HTTP shape will pinch. The WS
stream is the escape hatch; prefer 202-plus-event for any future long
operation rather than extending the blocking pattern.

### ARCH-07 — `partyboxd` standalone is a product nobody has asked for yet
**Severity:** P3 · **Status:** ACCEPTED-RISK

The headless-daemon layer costs a package boundary, a second settings tree
(`PARTYBOXD_*` vs `COMPANION_*` — two env prefixes users must learn), and a
"which layer does this belong in" tax on every feature. The layering *inside*
the process is valuable; the claim that people will deploy `partyboxd` alone
is unproven. Keep the package split (it enforces the layering), but stop
spending effort on standalone ergonomics until a real user appears, and
consider collapsing the two env prefixes into one documented namespace before
the v1.0 API freeze — after freeze it's forever.

### ARCH-08 — FastAPI app version literal drifts from real version
**Severity:** P3 · **Status:** OPEN · **Where:** `partyboxd/api/app.py` (`version="0.1.0-dev"`)

`create_app` hardcodes `0.1.0-dev` into the OpenAPI metadata while
`/api/v1/health` reports `partyboxd.__version__` from package metadata. Use
`partyboxd.__version__` in both places; the ADR-019 "no version literal is
ever edited by hand" rule is violated by this one line.

---

## Part C — Assumptions worth stress-testing before v1.0

1. **"Capability-based means new models need no code"** — untested. FDDF
   payload offsets (ADR-027 admits: "if a future Harman model places the
   address at a different offset…"), opcodes, the `"PartyBox"` name-substring
   scan filter, and the `"JBL"` sanity guard are all single-model evidence.
   The claim is a hypothesis until a second model connects. Treat the model
   matrix (README) as the product's most important empty table.
2. **"BLE exclusive connection is an acceptable v1.0 trade-off"** — probably,
   but it means the JBL app (firmware updates, EQ, lighting — features you
   don't offer) is unusable while Companion runs, and the failure mode is a
   silent JBL-app connection error the user won't attribute to you.
   Documented in roadmap known-limitations; make sure it's in the README's
   user-facing section too, with the workaround.
3. **"Pi OS Bookworm + backports wireplumber 0.5.8 pin"** — the appliance now
   depends on a backports package staying available for a pinned version.
   When Trixie becomes the base, the entire ADR-028 WirePlumber analysis must
   be revalidated. Budget for it; don't let a base-image bump ride along in an
   unrelated release.
