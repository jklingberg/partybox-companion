# ADR-037: `GET /api/v1/health/details` — Exposing Supervisor Health

**Status:** Accepted

---

## Context

The Portal redesign's roadmap (`docs/design/portal-redesign.md` §12, item 3) called for exposing `Supervisor.health()` (M17.1, `companion/supervisor.py`) as an endpoint so the health sheet could show real per-task state instead of synthesizing a "Companion" row from whatever data happened to be reachable. This was deferred through both prior redesign PRs (#48, #49) as the last open item in §13's ship order.

`Supervisor.health()` already returned exactly the shape needed — a list of `TaskHealth` (name, state, timestamps, `last_exception: Exception | None`, `total_failures`) — with a docstring stating the intent explicitly: *"Future milestones will route this through `/api/v1/health` and Portal diagnostics without requiring changes to the Supervisor architecture."* This ADR is that milestone; `Supervisor` itself needed no changes.

Two decisions had no existing precedent to follow:

1. **Response shape** — `TaskHealth.last_exception` is a raw `Exception`, not JSON-serializable.
2. **Auth** — every other endpoint in `companion/services/router.py` is deliberately unauthenticated (per that module's docstring: "read-only appliance state and contain no sensitive data").

## Decision

### 1. Response shape: `last_exception` becomes one formatted string

```python
class TaskHealthResponse(BaseModel):
    name: str
    state: Literal["waiting", "running"]
    last_exception: str | None
    total_failures: int
```

`last_exception` is rendered as `f"{type(exc).__name__}: {exc}"` (`None` if the task never failed or its last exit was a clean, unexpected return) — the same format `Supervisor._supervise` already uses in its own log line, so the API and the journal describe a crash identically.

`TaskHealth.running_since` and `last_failure_at` are **not** included in the response. Both are `time.monotonic()` values, explicitly documented in `supervisor.py` as "not comparable across process restarts" — a browser client has no monotonic clock to compare them against, so surfacing them as raw floats would just be numbers the frontend can't do anything with. `state` + `total_failures` is enough to answer the only question the health sheet needs answered: *is this task down right now, and has it been flaky?* A task is "down right now" when `state == "waiting"` and `total_failures > 0` — `state` alone isn't enough, since every task starts in `"waiting"` for a moment before its first run.

If a future consumer needs "how long has it been down," that's a real gap to close then (most likely by adding wall-clock timestamps to `TaskHealth` itself, which would then flow through unchanged) — not a reason to ship monotonic floats today that no client can use.

### 2. Auth: required, unlike the rest of this router

`GET /api/v1/health/details` sits behind the same API-key dependency as partyboxd's private routes (`/api/v1/speaker`, `/api/v1/battery`, `/power/*`) — `partyboxd.api.auth.make_auth_dependency`, applied via `Depends(auth)` only on this one route in `make_services_router`. Every other endpoint in this router stays unauthenticated.

The distinction is what's exposed. `GET /api/v1/audio`, `/spotify`, `/volume`, and friends report coarse booleans and enums — "is Spotify running," "is audio connected" — the same category of thing `GET /api/v1/health` already reports publicly, and explicitly not sensitive. `last_exception` is different in kind: it's `str(exc)` off a real Python exception raised somewhere in `DeviceManager`, `AudioService`, `SpotifyService`, or one of the wiring coroutines in `__main__.py` — which can include things like BLE addresses, file paths, or subprocess output embedded in the exception message, depending on what failed. That's internal diagnostic detail, not appliance status, and it's exactly the shape of thing partyboxd's private routes already exist to gate.

Auth is a no-op unless the operator has actually set `PARTYBOXD_API__API_KEY` (`make_auth_dependency` passes every request through when `settings.api.api_key` is `None`) — so on the common unconfigured appliance this changes nothing observable. `companion/__main__.py` builds its own `make_auth_dependency(daemon_settings)` instance (a second one from the same settings partyboxd's own `create_daemon_app` already built one from) and passes it to `make_services_router`; `make_services_router`'s `supervisor` and `auth` parameters both default to `None` so every existing test call site (`test_spotify_api.py`, `test_volume_api.py`, etc.) is unaffected.

### 3. Frontend: real data replaces the synthesized "Companion" row

The health sheet's `healthItems()` (`webui/static/index.html`) previously derived the "Companion" row from `S.health` alone — `S.health ? 'ok' : 'err'` — which was really just "did the last poll succeed," always true once the Portal had loaded at all, and reset to the design doc's "synthesized view" framing (§12 item 3's stated problem). The `Speaker`/`Bluetooth Audio`/`Spotify Connect` rows already come from real per-domain endpoints (`/health`, `/audio`, `/spotify`) and are unaffected.

The new `companionHealthItem()` reads `S.healthDetails.tasks` (fetched alongside the other Promise.all calls in `refresh()`, with `withAuth: true`) and surfaces the first task that is currently backed off after a crash — `state === 'waiting' && total_failures > 0` — as `"<Humanized task name> is restarting (<N> failures)"`. Task names are humanized generically (`kebab-case` → `Title case prose`) rather than hardcoded per name, so a task added to `__main__.py`'s `supervisor.register()` calls in the future shows up sensibly without a frontend change. No recovery-action button is offered on this row: unlike Spotify's "Restart" action, the Supervisor already restarts the task automatically with backoff — there's nothing for the user to click that the appliance isn't already doing.
