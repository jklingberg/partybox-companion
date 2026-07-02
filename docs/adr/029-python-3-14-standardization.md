# ADR-029 — Python 3.14 Standardization

**Status:** Accepted
**Date:** 2026-07-02

---

## Context

Companion is an appliance, not a reusable library, and the project already avoids multi-version compatibility layers wherever possible (see the design filter in [docs/roadmap.md](../roadmap.md)). Before this change, `requires-python = ">=3.11"` was set project-wide, CI tested a 3.11/3.12 matrix, and the devcontainer, image build, and Pi deployment all tracked whatever Python Raspberry Pi OS Bookworm's `apt python3` package happened to ship (3.11.2).

Python 3.14 was evaluated to determine whether the project should standardize on a single, newer version instead of maintaining that spread.

## Decision

Standardize on **Python 3.14** everywhere: `pyproject.toml` (`requires-python`, ruff `target-version`, mypy `python_version`), a repo-root `.python-version` pin, CI, the devcontainer, and the Pi appliance image.

### Project policy, not just a one-time migration

PartyBox Companion intentionally supports **exactly one Python version**: the version pinned by the appliance image (currently 3.14, tracked by the repo-root `.python-version`). Backwards compatibility with older Python releases is not a project goal — this follows the same design filter the project already applies everywhere else (see `docs/roadmap.md`): one appliance, one supported configuration, no compatibility layers carried for their own sake.

A consequence of this: `requires-python = ">=3.14"` in the three `pyproject.toml` files is a **packaging-convention lower bound**, not a statement that 3.15, 3.16, etc. are supported. Only 3.14 is actively validated — against real dependency versions, against `mypy`/`ruff` targets, and on real appliance hardware. Newer Python releases require the same deliberate evaluation this ADR represents (compatibility check → migration PR → hardware validation) before the pin moves; they are not adopted implicitly just because `>=` permits them.

### Compatibility findings

- **Runtime dependencies** — `bleak` (3.0.2), `dbus-fast` (5.0.22), `fastapi`/`uvicorn`/`pydantic`/`pydantic-settings`/`structlog`/`typer`/`rich` all install and run cleanly under 3.14; `dbus-fast` ships official `cp314` wheels. `aiohttp` and `zeroconf`, mentioned as candidates for review, are not actually dependencies of this project.
- **Dev tooling** — `uv` has Tier 1 support for 3.14 (including free-threaded builds); `mypy` 2.1.0 explicitly supports 3.14; `ruff`, `pytest`, `coverage`, and `pre-commit` have no known issues.
- **One project-specific risk, verified clean:** PEP 649/749 makes deferred annotation evaluation the language default in 3.14 for *every* file, not just files opting in via `from __future__ import annotations`. This is the same general hazard behind the `dbus-fast` `@method()` introspection bug documented during M16 bonding work (`bluez_dbus.py` — see the M16 implementation notes), where `dbus-fast` reads raw `__annotations__` for D-Bus signature inference. The regression test added at the time (`test_pairing_agent_class_is_constructible`) exercises this exact path and passes under 3.14. `ruff --fix` also safely unquoted two forward references in `bluez_dbus.py` now that 3.14 doesn't require quoting them — neither touches the `@method()`-decorated D-Bus service methods.
- **Language change applied automatically:** bumping `ruff target-version` to `py314` reformatted five `except (A, B):` clauses to the new unparenthesized `except A, B:` form (PEP 758) via `ruff format`; no manual changes needed.

### Raspberry Pi appliance: Option A (uv-managed Python 3.14)

Raspberry Pi OS is pinned to Bookworm (Debian 12), whose `apt python3` package stops at 3.11 and will not reach 3.14 — Debian bumps its default Python once per OS release, and that's a separate, larger lever (`image/config/base-image.env`) than this decision. `uv` already manages its own pinned toolchain independently of apt (see [ADR-019](019-distribution-approach.md)'s uv version pin), so `image/install.sh` now runs `uv python install` (reading the repo-root `.python-version` pin) instead of installing `apt python3`. This keeps the same reproducibility story as the existing `UV_VERSION` pin — one deliberate, reviewable version bump — without waiting on a Debian release cycle.

**Rejected:** Option B (track whatever Python Pi OS Bookworm ships). Freezes the project on 3.11 indefinitely; the only way to move forward would be a full base-image bump, which is an unrelated, much larger change.

## Consequences

- `packages/*/pyproject.toml` classifiers now list only `3.14`; `>=3.11`/`3.12` support is dropped, matching the project's single-version philosophy.
- `image/install.sh` no longer installs `apt python3`; nothing else in the image depended on it.
- The devcontainer base image moved to `mcr.microsoft.com/devcontainers/python:3.14-bookworm` — the Debian 12 base is unchanged, only the Python interpreter.
- `docs/adr/019-distribution-approach.md` is left as-is (a historical record of the pipeline as designed); this ADR supersedes only the Python-version aspect of it.

## Hardware validation (2026-07-02, PartyBox 520 appliance)

Validated on the real appliance Pi, not just in the devcontainer/CI:

- `uv python install` fetched a native `cpython-3.14.6-linux-aarch64-gnu` build and the venv at `/opt/partybox-companion` rebuilt successfully against it.
- **Found and fixed a real bug in the process:** `uv python install`'s default install location is `$HOME/.local/share/uv/python`. `install.sh` runs as root during the image build, so that default resolves to `/root/.local/...` — mode `0700`, unreadable by the restricted `companion` service account (`ProtectHome`, no login shell — see ADR-018). Without a fix, `companion.service` fails at startup with `Permission denied` resolving the venv's `python` symlink. This never surfaced in the devcontainer (single unprivileged user, no separate restricted service account) or in CI (ephemeral root runner, no persistent service). Fixed by pinning `UV_PYTHON_INSTALL_DIR=/opt/uv/python` in `install.sh` before the interpreter install, landing it in a world-readable system path instead.
- With that fix, `companion.service` started cleanly under Python 3.14.6, reconnected BLE GATT to the real PartyBox 520 (`ble_connected: true`), and the A2DP retry loop drove real `dbus-fast` calls against system BlueZ, getting an expected rejection response rather than a Python exception — confirming no annotation-introspection regression in `bluez_dbus.py`.
- `test_pairing_agent_class_is_constructible` (the regression test from M16 covering the exact `dbus-fast` × annotation-evaluation hazard this ADR flagged as a risk) passes under 3.14.
- **Not exercised:** a fresh `Pair()` through the `org.bluez.Agent1` flow — the speaker was already bonded, and forcing an unpair/re-pair cycle to test this specific path was judged disruptive to a live appliance and out of scope for this change. `bluez_dbus.py`'s pairing flow was already unverified against real hardware before this migration (see M16 implementation notes); that gap is pre-existing and unrelated to Python 3.14.

### Lessons learned

The `UV_PYTHON_INSTALL_DIR` bug (above) is the clearest evidence in this migration for why appliance changes get hardware validation rather than stopping at CI:

- **Invisible in CI** — the GitHub Actions runner is ephemeral and root for the whole job; there's no restricted service account to fail against, and nothing restarts a systemd unit.
- **Invisible in the devcontainer** — it's a single unprivileged user (`vscode`) with no separate, more-restricted account standing in for `companion`.
- **Only reproducible under the actual production shape**: root running the installer, a distinct no-login system account running the service, `ProtectHome` in effect. That specific combination only exists on the real Pi.

The general principle: any change that touches how the appliance is installed or how the service account resolves its own runtime environment needs to be exercised as that account, on that OS, not just type-checked and unit-tested. This is the same category of gap M16 already flagged for `bluez_dbus.py`'s pairing flow (never hardware-verified) — worth keeping in mind for future install.sh or systemd-unit changes.

## Open items

- Devcontainer image build could not be verified from inside this session (no nested Docker access) — verify with a container rebuild before merging.
- The pre-existing gap in hardware verification of the `org.bluez.Agent1` pairing flow (fresh `Pair()`, not just reconnect) remains open — tracked separately in M16, not introduced by this change.
- Follow-up simplification opportunities enabled by 3.14 (e.g. dropping `from __future__ import annotations` across the 35 files that use it, once the annotation-evaluation risk above has more real-world mileage) are intentionally left out of this change — see the PR description.
