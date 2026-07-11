# 03 — Concurrency & Race Conditions

All findings verified against source, not inferred from docs. The codebase is
single-event-loop asyncio throughout (no threads except uvicorn's loop), which
eliminates data races on plain attributes; the hazards below are all
*interleaving* hazards across `await` points, or cross-writer file/state
hazards.

Positive note first: several classic races were **already found and closed**
by the team, with tests — the WS subscribe-before-forwarder-task race
(ADR-036), the BehaviorSubject TOCTOU on `audio_ready` (ADR-028), the
`_drain_inbox_sentinels` spurious-disconnect fix, and the `_connected_event`
lockstep sites (ADR-034). The review below assumes those stay locked in by
their tests.

---

### RACE-01 — Settings-save can erase a concurrently-paired speaker address
**Severity:** P1 · **Status:** OPEN · **Where:** `webui/router.py::put_config` + `services/pairing.py::_do_pair` + Portal `saveSettings()`

Two writers share `config.json` with whole-object PUT semantics and no
versioning. Sequence, fully user-reachable:

1. Portal loads; `S.config` has `audio_sink_address: null`.
2. User pairs the speaker → `_do_pair` persists the address (read-modify-write,
   atomic *within* the loop — fine on its own).
3. User opens Settings (form was seeded from stale `S.config` if the sheet was
   opened before the pairing event refreshed it, or simply if a refresh was
   missed) and hits Save → Portal PUTs `{...S.config, spotify_*}` → the stale
   `audio_sink_address: null` **overwrites the paired address**.
4. `AudioService` keeps its live copy until restart; after the next reboot the
   appliance boots unpaired. Delayed-fuse data loss.

**Fix:** server-side merge (treat absent/None fields as "unchanged") or a
dedicated `PATCH`; Portal re-GETs config on Settings open. Add a regression
test that interleaves a pairing persist with a config PUT.

### RACE-02 — Liveness probe and REST commands share one un-arbitrated notification stream
**Severity:** P2 · **Status:** OPEN · **Where:** `partyboxd/device/manager.py::_drain_with_health_check` / `_poll_liveness`; `partybox/device/capabilities/*`

While `_poll_liveness` awaits a battery/firmware response, any concurrent
writer adds traffic to the same inbox: a REST `POST /power/on` write elicits a
speaker ACK/state notification that lands mid-probe. Today the probe loops
skip undecodable frames so the *known* interleavings are benign, but the
correctness argument is "every frame the speaker can currently send happens to
be either decodable-and-expected or ignored" — a property nobody checks when
adding the next opcode. The moment two request/response capabilities can run
concurrently (e.g. BLE volume lands, Portal polls volume while the 15 s
liveness probe runs), responses cross-match: `firmware_version()` will happily
consume and discard a `0x9E` battery frame that `_poll_liveness` was waiting
for, and vice versa — two coroutines awaiting `transport.receive()`
concurrently steal from each other nondeterministically (asyncio.Queue has one
winner per item).

**Fix:** the ARCH-02 demultiplexer (route by opcode to pending futures). Until
then: a per-device `asyncio.Lock` around request/response exchanges would make
the invariant explicit instead of accidental.

### RACE-03 — `power_off` immediately followed by disconnect can mark the wrong failure
**Severity:** P3 · **Status:** ACCEPTED-RISK · **Where:** `manager.py::power_on/power_off` + ADR-034

`PowerChangedEvent` is emitted optimistically after the GATT write succeeds;
the speaker then drops BLE (~1–2 s later) and the manager emits
`DisconnectedEvent` → `SpeakerStateChangedEvent("off")`. Portal handles this
(POWERING scene). Noted only so nobody "fixes" the event order: per ADR-035
Rule 2 the write-success event is correct; the transient off is transport
recovery. No action.

### RACE-04 — Spotify gate's grace window drops rapid toggle sequences by design — verify one edge
**Severity:** P3 · **Status:** OPEN (needs a test) · **Where:** `companion/__main__.py::_gate_spotify_on_audio`

In GRACE the gate does one `queue.get(timeout=grace)`. Sequence
`False → True → False` inside the window: gate consumes `True` (recovery,
keeps librespot), returns to top, consumes `False`, re-enters grace — correct.
But sequence `False → True(recovery consumed) → [gate at top of loop]` where
the queue is empty and audio is *actually* down again with the `False` event
**dropped by a full queue** (64-slot bound, drop-newest, per ADR-036): the
gate believes audio is up indefinitely; librespot stays advertised with no
sink. Requires 64 unconsumed events — implausible today, but the gate is the
one subscriber where a dropped event has a *stuck* consequence rather than a
cosmetic one (everything else reconciles via polling; the gate has no
reconcile path). Cheap hardening: after grace-recovery, cross-check
`audio.audio_ready` property once. Add a unit test either way.

### RACE-05 — `update_settings` during `_terminate` can leak a just-spawned librespot
**Severity:** P3 · **Status:** OPEN · **Where:** `companion/services/spotify.py`

`update_settings()` terminates `self._proc` if present. Window: `_run_once`
has passed the `create_subprocess_exec` await but not yet assigned
`self._proc = proc` (they're adjacent, but `create_subprocess_exec` itself is
the await). A REST `spotify/restart` landing in that window terminates the
*old* proc reference (None → no-op) and the new librespot starts with stale
settings; the restart silently doesn't apply until the next natural exit.
Cosmetic-severity (user hits Restart again), but the fix is one line: have
`_run_once` check a settings generation counter after spawn and self-restart
if it changed.

### RACE-06 — WS handler blocks all sources on one slow client, with an unbounded buffer behind it
**Severity:** P3 · **Status:** OPEN · **Where:** `partyboxd/api/ws.py::ws_events`

The per-connection loop is `merged.get() → await websocket.send_json(...)`. A
client that stops ACKing TCP stalls `send_json`; forwarders keep
`put_nowait`-ing into the unbounded `merged` (DEBT-11). Per-connection, so
other clients are unaffected, and the heartbeat timeout doesn't help (it only
fires on *idle*). Combined fix with DEBT-11: bounded merged queue +
`send_json` wrapped in a timeout that closes the connection.

### RACE-07 — Provisioning `_do_connect` failure classification races NM's async state
**Severity:** P3 · **Status:** ACCEPTED-RISK · **Where:** `companion/services/provisioning.py`

`nmcli device wifi connect` exit code arrives before NM has necessarily
settled (rc=0 with a connection that drops seconds later, e.g. DHCP failure
post-activation). The service reports CONNECTED and tears down the AP; if the
STA then fails, the appliance is headless until reboot re-runs provisioning
(run() only checks at startup). Mitigation exists (user power-cycles), but a
post-CONNECTED verification (poll `_is_sta_connected` for ~30 s before
declaring victory / before the run loop parks forever on `Event().wait()`)
would close it. Note also: once CONNECTED, the service **never re-enters
provisioning if WiFi is later lost or the router's password changes** — the
only path back to AP mode is a reboot with no valid credentials. Worth a
dedicated finding in v2 planning (roadmap doc, "provisioning re-entry").

### RACE-08 — BleakTransport replaces `_inbox` on connect while a stale reader could hold the old queue
**Severity:** P3 · **Status:** ACCEPTED-RISK · **Where:** `partybox/bluetooth/bleak_transport.py::connect`

`connect()` assigns a fresh `asyncio.Queue`. Any coroutine still parked in
`receive()` on the *old* queue waits forever (its sentinel went to the old
queue on the previous disconnect — actually `disconnect()` sentinels the old
queue first, so the ordering is safe **iff** callers always observe the
disconnect before reconnecting). `PartyBoxDevice`/`DeviceManager` serialize
connect/receive so this can't fire today; it's a landmine only for direct SDK
users. Document the single-reader contract on `ControlTransport.receive()`
(it's half-documented: "a single logical consumer is assumed") — or fix
structurally with ARCH-02.

### RACE-09 — Supervisor `os._exit(1)` in `"exit"` mode skips all cleanup
**Severity:** P3 · **Status:** ACCEPTED-RISK · **Where:** `companion/supervisor.py`

`os._exit` bypasses `finally` blocks: librespot child (mitigated by
PR_SET_PDEATHSIG — good), uvicorn sockets, in-flight config writes. No task
uses `"exit"` mode today. Add a docstring warning that any future `"exit"`
task must tolerate hard-kill semantics, and prefer `sys.exit` →
`Restart=on-failure` if clean teardown ever matters.

---

## Testing directive

Each OPEN finding above should gain a regression test when fixed
(RACE-01 and RACE-04 are testable today with the existing fixtures). Add them
to `packages/companion/tests/unit/` / `packages/partyboxd/tests/unit/`
following the existing naming pattern.
