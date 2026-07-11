# 02 — Hidden Technical Debt Register

Debt that does not announce itself in the roadmap or ADRs. Items already
tracked elsewhere (INC-2, FAULT-05, stale CLAUDE.md sudoers claim) are listed
at the bottom for completeness but not re-argued.

---

### DEBT-01 — No update mechanism at all
**Severity:** P0 (product) · **Status:** OPEN

The appliance disables `apt` timers and unattended-upgrades (ADR-020, correct)
but ships **nothing in their place**. There is no OTA path, no "update
available" signal, no documented upgrade procedure short of reflashing —
which destroys the user's pairing, WiFi credentials, and config, i.e. the
full first-boot ceremony again. ADR-017 says "upgrade path is straightforward:
replace `/opt/partybox-companion/`, restart" — true and implemented nowhere:
no script, no Portal surface, no release artifact for it (releases ship only
the full `.img.xz`).

Consequences compound: every security fix you ever ship (see
[04-security-review.md](04-security-review.md)) reaches only new flashes;
the installed base stays vulnerable forever. For a network-connected Linux
box this is the single most consequential missing feature.

**Remediation path (see roadmap doc):** (1) release a versioned tarball of the
venv alongside the image; (2) a Portal-triggered updater (download, verify
SHA256+signature, swap `/opt` atomically, restart service); (3) a daily
version-check against GitHub releases API with a Portal banner — opt-out, and
doubling as the project's only (privacy-sane) install counter.

### DEBT-02 — librespot playback state cannot transition to "paused"
**Severity:** P2 · **Status:** OPEN (known, but understated) · **Where:** `companion/services/spotify.py`

Documented in-code: librespot 0.8 logs nothing on pause/stop, so `active`
latches `true` until the process restarts. Today it only mis-renders the
Portal ("Playing" while paused) — but ADR-038's idle shutdown deliberately
chose *speaker standby* over *playback state* as the idle signal partly
because this signal is unreliable. Any future feature that wants playback
state (auto-source-switching for AirPlay, "now playing" display, idle logic
v2) hits this wall. The robust fix is cheap and known: librespot's
`--onevent` hook (it invokes a program with `PLAYER_EVENT=playing|paused|…`)
or the PipeWire node state. Do it once; delete the stderr-pattern guessing.

### DEBT-03 — Config writes are non-atomic and last-writer-wins
**Severity:** P2 · **Status:** OPEN · **Where:** `companion/config_store.py::write`, `webui/router.py::put_config`, `services/pairing.py::_do_pair`

`write_text` truncates-then-writes: power loss mid-write corrupts
`config.json`. The corruption *recovery* is excellent (quarantine + defaults,
built after a real incident) — but recovery-by-losing-the-user's-settings when
a 5-line atomic write (`tempfile` + `os.replace` + optional fsync) prevents
the loss entirely is the wrong trade on SD cards, which fail exactly this way.

Separately, `PUT /api/v1/config` is whole-object replace with no concurrency
control, and two writers exist (Portal, PairingService). The concrete lost
update: open Settings → pair the speaker (pairing writes
`audio_sink_address`) → save Settings (Portal PUTs its stale pre-pairing copy,
`...S.config` spread) → **the just-paired address is erased** and the Portal
drops back to the PAIR scene. The Portal's carry-forward comment shows the
hazard was seen but only half-mitigated.

**Fix:** atomic write; make the API a PATCH-merge (or have the server merge
absent fields); have the Portal re-GET config when opening Settings.

### DEBT-04 — `AudioService._disconnect()` contradicts the project's own isolation rule
**Severity:** P2 · **Status:** OPEN · **Where:** `companion/services/audio.py::_disconnect`

Every A2DP *connect/check* goes through the subprocess because in-process
`dbus-fast` "returns wrong values without raising" (ADR-028) — but the
*disconnect* path calls in-process `BluezClient.disconnect_a2dp()` while bleak
is fully active, on the retry hot path after most failed connects. The
justification ("not timing-critical, conflict only manifests under concurrent
bleak traffic") describes precisely the conditions this call runs under. If
the conflict is real, this call can silently no-op — and a failed disconnect
here re-creates the WirePlumber endpoint-churn cascade ADR-028 spent a page
diagnosing. Resolve together with ARCH-01: one broker, all calls through it.

### DEBT-05 — Frame reassembly is a per-call afterthought instead of a transport property
**Severity:** P2 · **Status:** OPEN · **Where:** `partybox/protocol/codec.py::FrameReassembler`, used only in `BatteryCapability.status`

Only the battery path reassembles fragments. `firmware_version()` decodes raw
notifications directly — works today because that response fits one MTU, but
any future capability with a >MTU response silently truncates, and a battery
call abandoned on timeout leaves orphan fragments in the shared inbox for the
next consumer. The reassembler belongs at one place: the device's (future)
single reader (ARCH-02). Also note the resync heuristic (`buf.find(SOF)`)
can false-sync on a 0xAA payload byte after a lost header — acceptable, but
document it in the class docstring so nobody "fixes" a symptom of it blind.

### DEBT-06 — The Portal is a 1,646-line single file with zero automated coverage
**Severity:** P2 · **Status:** OPEN · **Where:** `companion/webui/static/index.html`

The no-build-step constraint is a *good* product decision (must render from
the appliance's own AP). But the file now contains a scene router, a state
store, a WS client, three flows, and a template system in string literals —
and nothing in CI even *parses the JavaScript*. A typo in `onEvent` ships. The
mock mode (`?mock&state=off|standby|pair|degraded|provision`) is a purpose-
built test substrate that no test uses. See TEST-01. Independently: consider
splitting the `<script>` into `/assets/app.js` (still no build step, still
self-hosted) purely so diffs and reviews stop being 1,600-line-file diffs.

### DEBT-07 — Two env-var namespaces for one process
**Severity:** P3 · **Status:** OPEN · **Where:** `partyboxd/config`, `companion/config.py`, `system/systemd/companion.env`

Operators must know that the API key is `PARTYBOXD_API__API_KEY` but the port
is `COMPANION_PORT`, in the same process, same env file. Freeze-sensitive:
after v1.0 this split is permanent. Either alias `COMPANION_*` equivalents for
the daemon settings companion actually consumes, or document the split
prominently in `companion.env` (it is partially documented today).

### DEBT-08 — `HealthResponse.version` reports partyboxd's version as the appliance's
**Severity:** P3 · **Status:** OPEN · **Where:** `partyboxd/api/routes.py`, Portal "Running · v…"

All three packages version-lock today so it's invisible, but the Portal
displays "the appliance version" sourced from `partyboxd.__version__`. If the
packages ever version independently (ADR-002 allows it) the Portal lies. Add
`companion.__version__` to health when running as the appliance, or commit in
writing to lockstep versioning forever.

### DEBT-09 — `raspotify` third-party apt repo remains configured in the shipped image
**Severity:** P3 · **Status:** OPEN (verify) · **Where:** `image/install.sh`

librespot arrives via the raspotify repo (ADR-019). Check whether the repo and
its signing key remain in `/etc/apt/sources.list.d/` on the final image. With
apt timers disabled it's inert, but it is an extra trusted key in the trust
store of every shipped device, and ADR-016's requirement was "librespot ships
as part of Companion" — pinning + vendoring the single binary (with checksum)
into the image and *removing* the repo is cleaner. Also: nothing pins the
librespot version; two image builds a month apart can ship different librespot
versions with different stderr formats (see DEBT-02's parsing dependency).

### DEBT-10 — Idle-shutdown watcher state does not survive its own crash-restart
**Severity:** P3 · **Status:** OPEN · **Where:** `companion/__main__.py::_idle_battery_shutdown`

The ADR-038 incident proved the failure shape: `power_off()` raised, the
Supervisor restarted the watcher, and the restart **reset `idle_since` and
`last_known_on_battery`**. After a restart with the speaker already dark
(`"off"`, no battery reading obtainable) `last_known_on_battery` can never be
re-confirmed, so the shutdown never fires — precisely the battery-drain
scenario the feature exists to stop. Catch and retry `power_off()` failures
inside the loop (with backoff) rather than letting them crash the watcher, or
persist `last_known_on_battery` on the DeviceManager snapshot where it
survives.

### DEBT-11 — WS `merged` queue is unbounded per connection
**Severity:** P3 · **Status:** OPEN · **Where:** `partyboxd/api/ws.py::ws_events`

Per-source subscriber queues are bounded (64, drop-newest) but the merged
queue they forward into is `asyncio.Queue()` — unbounded. A WS client that
stops reading while events keep flowing grows that queue without limit
(slowly, given event rates; still the only unbounded buffer in the process).
Bound it the same way (drop on full) for symmetry.

### DEBT-12 — Event-driven refresh does full REST fan-out on BLE events
**Severity:** P3 · **Status:** OPEN · **Where:** `webui/static/index.html::onEvent`

`connected` / `disconnected` / `speaker_state_changed` / `power_changed` each
trigger a full `refresh()` — five parallel REST calls plus conditional
speaker/battery calls — while the ADR-036 events that carry payloads update
`S` directly. During the ADR-034 power-cycle churn (off → reconnect → on) a
burst of state events multiplies into dozens of HTTP requests against a Pi
Zero-class CPU. Extend the payload-carrying pattern to the BLE events
(`speaker_state_changed` already carries `state`; `connected` carries
address/firmware/battery) and drop `refresh()` from those handlers.

---

## Already-tracked debt (for cross-reference only)

- **INC-2** — 40 % sink volume; product fix on M19 punch list (subsumed by ARCH-04 here).
- **FAULT-05** — stale-bond end-to-end hardware leg; carried to M19 fresh-pairing validation.
- **CLAUDE.md stale sudoers claim** — flagged in RC13 report; ADR-038 confirms no sudoers file exists. Fix the doc.
- **uv binary SHA256 verification** — deferred in ADR-019; do it at the next uv bump.
- **`bluez_dbus.py` fresh-`Pair()` path never hardware-verified** — M19 goal, keep it a release gate.
