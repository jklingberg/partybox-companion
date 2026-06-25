# ADR-002: Monorepo with uv Workspace

**Status:** Accepted

---

## Context

The project has three distinct components with a strict dependency order:

1. `partybox` — Bluetooth SDK. No daemon, no REST.
2. `partyboxd` — Headless daemon. Depends on the SDK.
3. `companion` — Full appliance. Depends on the daemon.

These could be maintained as separate repositories. The alternative is a monorepo with a workspace tool managing cross-package dependencies.

## Decision

Single repository, three packages, managed as a [uv workspace](https://docs.astral.sh/uv/concepts/workspaces/).

The packages live in `packages/partybox/`, `packages/partyboxd/`, and `packages/companion/`. A root `pyproject.toml` configures shared tooling (ruff, mypy, pytest).

## Consequences

**Benefits:**
- Cross-package refactoring is a single commit. Renaming a type shared across all three packages takes one PR, not three coordinated PRs.
- CI runs all three packages in one pipeline. A change to the SDK that breaks the daemon is caught immediately.
- Shared tooling configuration (ruff, mypy, pre-commit) lives in one place.
- `uv sync --all-extras` installs everything; contributors do not need to understand the workspace structure to get started.

**Accepted trade-offs:**
- All packages are developed and released together. Independent versioning is possible but requires extra coordination.
- The repository is larger and less focused than a single-purpose library repo.
- Contributors working only on the SDK still clone the full repository.

**Alternative considered:** Separate repositories with the SDK published to PyPI and the daemon/companion declaring it as a dependency. Rejected because cross-cutting changes (which are common early in development) require coordinating multiple PRs and releases.
