# 05 — Testing Gaps

The suite is 442 unit tests, protocol tests use real-capture byte fixtures,
services mock subprocess/D-Bus cleanly, and `mypy --strict` passes on all three
packages. That is a strong baseline — better than most pre-1.0 projects. The
gaps below are about *what class of bug the current suite structurally cannot
catch*, ranked by how likely that bug is to reach a user.

---

### TEST-01 — The Portal (1,646 lines of behaviour) has zero automated coverage
**Severity:** P1 · **Status:** OPEN

`deriveScene()`, `onEvent()`, `handlePairingProgress()`, `sendPower()`, the WS
reconnect, the provisioning poll loop — none are exercised by any test. This is
the layer the user actually touches, it contains the two shipped UX bugs in
[06-ux-review.md](06-ux-review.md), and it has a **purpose-built test substrate
already in the code** (`?mock&state=...`) that nothing uses. A single `onEvent`
typo ships silently.

**Cheapest high-value fix:** extract the pure logic (`deriveScene`, the
`S`-reducers in `onEvent`, `humanizeTaskName`) so it's unit-testable without a
DOM — these are already pure functions of `S`. Then, if budget allows, a
Playwright smoke pass against `?mock` states (the RC13 run already used
Playwright against the live appliance, so the tooling is in-house). Even just
node-parsing the extracted logic in CI catches syntax errors.

### TEST-02 — No integration test spans two real components
**Severity:** P1 · **Status:** OPEN

Every test mocks the layer below. `DeviceManager` tests mock the SDK; API tests
mock `DeviceManager`; the gate test mocks `AudioService` events. Nothing
asserts that a real `PartyBoxDevice` + `MockTransport` drives a real
`DeviceManager` drives a real FastAPI response — the integration seams
(the health-check drain/probe choreography, the event fan-in from three real
services into the real WS handler) are exactly where the subtle bugs live
(RACE-02, RACE-04) and are exactly what's mocked away.

**Recommendation:** a small `tests/integration/` tier (non-hardware) that wires
`MockTransport → PartyBoxDevice → DeviceManager → create_app → httpx AsyncClient`
and drives real event flows. The `MockTransport` (ADR-002/M2) was explicitly
built to make this possible ("good enough that the entire protocol and device
layers can be developed and tested without real hardware") — that promise is
half-redeemed; the daemon integration half isn't.

### TEST-03 — The concurrency fixes have tests; the concurrency *hazards* do not
**Severity:** P2 · **Status:** OPEN

The team tests races it already found (WS start race, ordering). The open ones
in [03-concurrency-and-races.md](03-concurrency-and-races.md) are untested:
RACE-01 (config PUT vs pairing persist — trivially reproducible with the
existing `ConfigStore` fixtures), RACE-04 (gate + dropped event), RACE-05
(update_settings mid-spawn). Add a regression test with each fix; RACE-01's is
writable today independent of any fix and would document the current bug.

### TEST-04 — No test asserts the auth boundary of the services router
**Severity:** P2 · **Status:** OPEN · **Where:** `test_spotify_api.py`, `test_volume_api.py`, `test_health_details_api.py`

`test_health_details_api` covers the one authenticated route. Nothing asserts
the *negative*: that factory-reset / wifi-connect / config-PUT are reachable
without a key (they are — SEC-02). When SEC-02 is fixed, these need tests that
assert the new auth requirement; today there's no test that would even notice
the surface is open. A test encoding "these endpoints require auth when a key
is set" is the executable form of the SEC-02 fix.

### TEST-05 — Error/degradation paths are thin relative to happy paths
**Severity:** P2 · **Status:** OPEN

`test_audio.py` (46 tests) covers the flap/failure/backoff logic well — good.
But: `ConfigStore` corruption-recovery quarantine is tested?
(`test_config_store.py` exists — verify it covers the quarantine + defaults
path, which was built after a real incident and is load-bearing for
"appliance always boots"). The `_a2dp_connect.py` subprocess protocol
(`error_code`, `STALE_BOND` classification) — is the *parsing* contract tested
independent of a live bus? The `_classify_nmcli_error` keyword matching is
fragile across nmcli versions and drives user-facing WiFi error messages;
it needs fixture-based tests with real nmcli stderr strings (the same way the
protocol layer uses real captures).

### TEST-06 — Time-and-backoff logic is tested with real sleeps or not at all
**Severity:** P3 · **Status:** OPEN (verify)

The idle-shutdown thresholds (30 min / 90 s), flap windows (20 s), grace
period (300 s), and reconnect backoff caps are the kind of logic that's easy to
get off-by-one and hard to test without controlling the clock. Confirm
`test_idle_battery_shutdown.py` and `test_audio.py` inject a fake clock /
patch `time.monotonic` rather than sleeping — if they sleep, they're slow and
under-cover the boundaries; if they don't test boundaries at all, the
"standby → off threshold switch mid-count" logic (ADR-038's most intricate
piece) is unverified.

### TEST-07 — Hardware coverage is single-model, and the suite knows it
**Severity:** P2 (product) · **Status:** ACCEPTED-RISK, but name it

`test_hardware.py` runs against whatever PartyBox is present; the validation
matrix (README) has exactly one row. This isn't a unit-test gap — it's the
product's central empirical gap (see [01-architecture-review.md](01-architecture-review.md)
Part C). The FDDF offset, opcodes, name filters, and the whole capability
model are single-model evidence. No amount of mocking substitutes; the ask is
a second physical model before claiming "capability-based, works on any model."
Track it as a release-notes honesty item, not a code fix.

### TEST-08 — No test exercises the fresh-`Pair()` D-Bus agent path
**Severity:** P1 (already a release gate) · **Status:** OPEN

`bluez_dbus.py`'s `org.bluez.Agent1` flow (`Pair()`, agent registration,
first-time bonding) has never run on hardware — only reconnect-to-bonded has.
This is the M19 fresh-pairing goal and it *is* the highest-risk unverified
path in the codebase (a brand-new user's very first action). The
`test_pairing_agent_class_is_constructible` regression test guards the PEP-563
introspection hazard but not the flow. Keep this a hard v1.0 gate; the ADR-038
`call_PowerOff`→`call_power_off` incident is direct proof that dbus-fast
method-name generation bugs survive both mypy and hand-rolled fakes and only
surface on a real bus.

---

## Coverage-shape summary

| Area | Coverage | Gap |
|---|---|---|
| Protocol codec | Excellent (real captures) | none material |
| SDK device/capabilities | Good (MockTransport) | concurrent-use invariants (RACE-02/08) |
| DeviceManager | Good | integration with real SDK + real API (TEST-02) |
| Services (audio/spotify/pairing/provisioning) | Good (subprocess/D-Bus mocked) | nmcli/subprocess parsing fixtures (TEST-05); fresh-pair on hw (TEST-08) |
| REST API | Good for partyboxd routes | services-router auth negative tests (TEST-04) |
| WebSocket fan-in | Good (ADR-036 tests) | multi-source integration (TEST-02/03) |
| **Portal / frontend** | **None** | **everything (TEST-01)** |
| Cross-component integration | **None** | **TEST-02** |
| Hardware matrix | Single model | second model (TEST-07) |
