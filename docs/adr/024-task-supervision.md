# ADR-024 — Task Supervision and the Two-Tier Recovery Model

**Status:** Accepted
**Date:** 2026-06-30
**Milestone:** M17.1

---

## Context

The appliance runs five long-lived asyncio tasks concurrently: the BLE device
manager, the Spotify Connect service, the A2DP audio service, the BLE volume
forwarder, and the provisioning service.

Before M17.1, each task was created with `asyncio.create_task()` and stored as
a local variable.  If any task exited with an unhandled exception, the exception
was stored in the `Task` object and silently discarded.  The process continued
running; the Portal remained accessible; the failed component simply stopped
functioning.

This is the **silent failure** problem: the appliance appeared healthy while a
critical subsystem was dead.  Recovery required manual `systemctl restart
companion` — user intervention that violates the appliance contract.

---

## The two-tier recovery model

Recovery happens at two levels, and the two levels have different
responsibilities:

**systemd** — *process-level recovery.*  If the entire `companion` process
crashes, systemd restarts it with `Restart=on-failure`.  This is the outer
envelope: it handles the case where in-process recovery has completely failed.
It is not designed for, and should not be burdened with, the case where a single
background task exits silently inside an otherwise healthy process.

**Supervisor** — *task-level recovery.*  Within a running process, the
supervisor detects when any registered task exits unexpectedly and relaunches it
according to a configurable restart policy.  This is the inner envelope: it
handles transient failures that do not crash the process.

The two tiers are complementary.  Neither replaces the other.  A process crash
that systemd catches is a failure that the supervisor was unable to prevent; a
task-level restart that the supervisor handles is a failure that systemd never
needs to see.

---

## Why services do not own their own restart logic

The obvious alternative is to add a restart loop inside each service — a `while
True` that catches exceptions and retries.  Some services already do this for
*expected* domain failures: `SpotifyService` restarts librespot when it exits;
`AudioService` reconnects when A2DP drops.  Those loops are correct — they
handle failure modes that are part of the service's normal operating envelope.

The gap is *unexpected* failures: an unhandled exception that escapes the
service's own loop.  These failures are bugs; the service's defensive code did
not anticipate them.  Adding another try/except around the outer `while True`
only moves the problem one level up.  If the outer loop itself crashes —
because, for example, a dependency raises an unexpected type — the loop is gone
and nothing remains to restart it.

Critically: a service that has exited cannot restart itself.  The recovery code
must live somewhere outside the service, or it cannot run at all.

---

## Why supervision is centralised

Embedding restart logic inside every service has a compounding problem:
inconsistency.  When each service implements its own backoff, its own crash
logging, and its own health state, they will diverge.  Prior art in this
codebase already shows two different approaches (`SpotifyService` vs
`AudioService`).  Inconsistency means gaps: one service backs off, another does
not; one logs at ERROR, another swallows the exception; one surfaces failure in
the Portal, another is invisible.

A single supervisor provides:

- **Consistent policy.** All tasks back off the same way.  Changing the
  defaults means changing one place.
- **Consistent observability.** All failures are logged with the same format.
  Health state is collected uniformly.
- **A single extension point.** Circuit breakers, systemd watchdog heartbeats,
  Portal diagnostics, and task dependency tracking all extend the supervisor
  rather than N services.

---

## Separation of responsibilities

Services remain responsible for their own domain logic.  The supervisor does
not know — and must not need to know — what `DeviceManager.run()` does
internally.  It only observes the boundary: did the coroutine exit?

The division is:

| Responsibility | Owner |
|----------------|-------|
| BLE reconnect logic | DeviceManager |
| librespot lifecycle | SpotifyService |
| A2DP reconnect timing | AudioService |
| Detecting unexpected exits | Supervisor |
| Restart policy and backoff | Supervisor |
| Task health state | Supervisor |
| Process-level restart | systemd |

Services handle failures within their domain (expected conditions such as BLE
disconnect or librespot crash) as part of their own `run()` loop.  The
supervisor handles failures that escape those loops — failures the service did
not anticipate.

---

## Restart policy

The `RestartPolicy` dataclass expresses intent independently of which task it
applies to.  The policy defaults (5 s initial delay, 300 s cap, ×2 backoff,
60 s stability threshold) apply uniformly to all tasks unless overridden.

Two recovery modes:

- **`"restart"`** — re-launch after backoff.  Correct for all five current
  tasks, whose failures are expected and transient.
- **`"exit"`** — call `os._exit(1)` so that systemd performs a clean process
  restart.  Reserved for tasks whose failure domain cannot be healed in-process.
  No current task uses this mode; it exists for future tasks such as startup
  migrations or cases where a persistent hardware fault makes indefinite retry
  harmful.

A task running stably for at least `reset_after` seconds before exiting has its
backoff reset to `initial_delay`.  This prevents a task that usually works from
accumulating a long delay after an isolated failure.

---

## Health tracking

The supervisor collects health state for every registered task — running/waiting
status, last failure timestamp, last exception, and cumulative failure count —
regardless of whether that state is exposed externally.

Collecting state now is cheap.  Retrofitting instrumentation later requires
intrusive changes to `_supervise` and risks introducing bugs in the recovery
path.  The `Supervisor.health()` method provides a clean seam for M17.2 and
later milestones to read this state without any changes to the supervisor
architecture:

- **M17.2** — Portal diagnostics surface task health via the existing status UI.
- **M17.3** — `Supervisor.health()` feeds the systemd watchdog heartbeat.
- **Future** — `/api/v1/health` exposes the snapshot for external monitoring.

---

## Consequences

### Immediate

- Background task failures are always detected and logged.
- Every task restarts automatically with bounded backoff.
- `_run()` in `__main__.py` is simplified: five `asyncio.create_task` calls
  and five cancel/await blocks replaced by five `supervisor.register` calls and
  a single supervisor task.

### Deferred design questions

Two gaps in the current design are documented here rather than closed, because
closing them requires decisions that no current use case has forced:

**Task dependencies.** `_forward_ble_volume` subscribes to `DeviceManager`'s
event bus.  If `DeviceManager.run()` restarts, the existing subscription may
become stale.  The correct long-term model is explicit dependency declarations
(`depends_on=["device-manager"]`) so the supervisor can cascade restarts to
dependents.  This requires running-state tracking (already present) and will be
addressed when Spotify lifecycle gating (M17.4) forces the dependency graph.

**Circuit breaker.** The current model retries indefinitely at `max_delay`
intervals.  For the existing tasks this is correct — the user may fix the
underlying condition at any time.  A future `max_failures_before_exit` field on
`RestartPolicy` would close the loop for tasks where indefinite retry is
harmful, but no current task needs it.

---

## Alternatives considered

### Embed restart loops inside every service

Rejected.  Services that have exited cannot restart themselves.  Per-service
loops are inconsistent, do not cover unexpected failures that escape the loop,
and create N independent extension points instead of one.

### `asyncio.TaskGroup`

Rejected.  `TaskGroup` cancels all sibling tasks when one fails.  Tasks here
are independent; failure of `AudioService` must not kill `SpotifyService`.

### systemd alone (`Restart=on-failure`)

Rejected for task-level failures.  systemd only fires when the process exits.
A silently dead background task does not exit the process.  Treating every
task failure as a process crash would produce unnecessary restart churn for
transient domain failures (BLE disconnect, librespot exit) and prevent the
services that are still functioning from continuing to run.
