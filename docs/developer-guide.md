# Developer Guide

> **See also:**
> [Architecture](architecture.md) · [Developer Handbook](developer-handbook.md) · [Roadmap](roadmap.md) · [ADRs](adr/) · [Contributing](../CONTRIBUTING.md)

Welcome to partybox-companion. This guide is written for experienced developers who may not have used modern Python tooling before — if you're coming from Go, Rust, Java, or C#, you'll find the concepts familiar but the tools different.

---

## What this project is

partybox-companion turns a JBL PartyBox into a WiFi speaker using a Raspberry Pi. The full picture is in [docs/vision.md](vision.md), but the short version: flash an SD card, plug in the Pi, open a browser, start streaming Spotify or AirPlay. No JBL cloud account, no subscription.

To get there, we needed to understand the PartyBox's proprietary Bluetooth control protocol and build an independent implementation on top of it. That reverse engineering work is the foundation — everything else is a software appliance on top of it.

---

## How we work

A few principles that shape how this project is built, and why the code looks the way it does.

### SDK-first

The `partybox` package is a standalone, publishable Python library. The daemon and appliance consume it, but it has no knowledge of them. This forces clean boundaries and means the SDK can be used independently of the rest of the project.

Every decision about what belongs in the SDK has been made deliberately. See [ADR-003](adr/003-sdk-first.md) and [ADR-010](adr/010-sdk-scope.md).

### Hardware validation before abstraction

We don't build against assumptions. Every architectural claim that depends on hardware behaviour — can the Pi stream A2DP while maintaining a BLE control connection? what transport does the speaker actually use? — is validated with real hardware before we build on top of it.

M3 was dedicated entirely to answering the audio transport question before writing a line of daemon code. M2's Bluetooth transport design was revised after hardware showed the speaker uses BLE GATT, not RFCOMM as originally assumed. The revision is documented in [ADR-015](adr/015-bluetooth-control-transport.md). The wrong assumption is still in the ADR — not to embarrass anyone, but because the correction is the useful part.

### Reality over assumptions

Relatedly: if something doesn't match the code, the code is wrong. Protocol documentation describes observations from real hardware captures, not what we think the protocol should do. When we're uncertain, we say so in the docs.

### Architecture decisions are documented

When we make a significant architectural decision — what transport to use, what belongs in the SDK, how to order milestones — we write an ADR. ADRs live in [docs/adr/](adr/) and are indexed in the [README](adr/README.md).

This matters more than it sounds. The reasoning behind a decision decays faster than the decision itself. Future contributors (including future-you) can read the ADR and understand why the code is the way it is, rather than guessing or undoing a decision that was made for a good reason.

### Milestone-driven implementation

Work proceeds in milestones, each with a clear completion criterion. The [roadmap](roadmap.md) describes the milestones in order and explains why they are ordered the way they are.

The milestone ordering is deliberate. We don't implement what we can't yet test or validate. We don't build UI before the API it consumes exists. We don't add features that the current milestone doesn't need.

### Reverse engineering as an interoperability tool

The protocol analysis is means, not end. We analyse the JBL app to understand how to talk to the speaker, then write an independent implementation. This is similar to how Samba implemented SMB or Wine implemented Win32 — clean-room interoperability work, not code copying.

[CONTRIBUTING.md](../CONTRIBUTING.md#legal-hygiene) covers the legal hygiene in detail. The short version: document observations, never transcribe or paraphrase proprietary source.

### Claude Code as an engineering assistant

Claude Code is used actively in this project for implementation, refactoring, and documentation. It is an engineering assistant — it accelerates execution, but it is not an architectural authority.

Architectural decisions go through the ADR process regardless of how they were explored. Claude Code helps implement what has been decided; it doesn't decide. Code review still matters — generated code has the same failure modes as any other code (wrong abstractions, subtle bugs, unnecessary complexity) and needs the same scrutiny.

When working with Claude Code on this project, expect to:

- Discuss architecture before implementation
- Review generated code carefully, particularly at boundaries between packages
- Write the ADR yourself after making a decision (Claude can draft it, but the reasoning should be yours)
- Use `uv run mypy` and `uv run pytest` to verify correctness, not just that the code looks plausible

---

## Getting started

The fastest path to a working environment is the dev container. Open the repository in VS Code, accept the prompt to reopen in a container, and wait about two minutes for the first build. After that, `uv run pytest` and every other command in this guide work immediately.

See [Developer Handbook — Development environment](developer-handbook.md#development-environment) for detailed setup instructions, including the native (non-container) path if you prefer it.

---

## Project toolchain

### uv

[uv](https://docs.astral.sh/uv/) is the Python package manager and virtual environment tool. If you're coming from another language, think of it as combining the roles of npm (or Cargo), the virtual environment, and the test runner wrapper.

**What problem it solves:**

Traditional Python tooling splits these concerns across multiple tools: `pip` for installation, `venv` for isolation, `pip-compile` or `poetry` for lock files. Each has its own conventions and failure modes. `uv` does all of it in one tool, significantly faster than any of them individually.

**Why we chose it over pip + venv:**

Speed is part of it — `uv sync` resolves and installs the entire workspace in a few seconds, not minutes. But the bigger reason is the workspace support: uv manages all three packages (`partybox`, `partyboxd`, `companion`) as a single workspace with a shared lockfile (`uv.lock`), so you never run into version mismatches between packages.

**What you need to know:**

```bash
# Install all packages and dev dependencies (run this after cloning, or after
# adding a dependency)
uv sync --all-extras

# Run any command in the virtual environment without activating it
uv run pytest packages/partybox/ -m "not hardware"
uv run python examples/power_on.py
uv run ruff format .

# Add a dependency to a specific package
cd packages/partybox
uv add bleak

# Add a dev-only dependency
uv add --dev pytest-asyncio
```

The virtual environment lives at `.venv/` in the repository root. VS Code discovers it automatically via the dev container configuration.

**What you don't need to do:** activate the virtual environment manually. `uv run` handles it. If you're used to `source .venv/bin/activate`, you can still do that — but you don't have to.

---

### Ruff

[Ruff](https://docs.astral.sh/ruff/) handles code formatting and linting. It replaces `black` (formatter), `flake8` (linter), `isort` (import sorting), and several `flake8` plugins — all in one tool, much faster than any of them.

**Why we chose Ruff:**

One configuration block in `pyproject.toml`, one command to run, one VS Code extension. Before Ruff, a typical Python project needed five or six linting tools configured to not contradict each other. Ruff made that a solved problem.

**Common commands:**

```bash
# Format all files (like gofmt or rustfmt — opinionated, no arguments needed)
uv run ruff format .

# Check for lint issues
uv run ruff check .

# Fix auto-fixable issues (import sorting, simple style fixes, etc.)
uv run ruff check --fix .
```

Ruff runs automatically on save in the dev container (via the VS Code extension). It also runs as a pre-commit hook before every commit.

**What Ruff enforces in this project:**

The enabled rule sets are in [pyproject.toml](../pyproject.toml) under `[tool.ruff.lint]`. Notable ones:
- `ANN` — annotations required on all public functions (this pairs with mypy)
- `S` — security checks via flake8-bandit (SQL injection, subprocess misuse, etc.)
- `B` — flake8-bugbear: common bug patterns and non-obvious style issues

---

### mypy

[mypy](https://mypy.readthedocs.io/) is Python's most widely used static type checker. This project runs it in `strict` mode, which means:

- All function signatures must have type annotations
- Return types must be explicit
- `Any` types must be explicitly acknowledged
- Optional values must be checked before use

**Why strict mode:**

Python's type system is opt-in and gradual by default — you can add types incrementally. Strict mode turns this into a requirement. For a project like this, where the protocol layer handles binary data and the device layer exposes typed capabilities to consumers, type errors are real bugs. Strict mode catches them at development time rather than at runtime on a Raspberry Pi connected to a speaker.

The strictness also matters for the SDK specifically. The `partybox` package is designed to be published and used by others — a typed public API is a real deliverable.

**How to run it:**

mypy must be run per-package because each package has its own `src/` layout:

```bash
cd packages/partybox  && uv run mypy src/ && cd ../..
cd packages/partyboxd && uv run mypy src/ && cd ../..
cd packages/companion && uv run mypy src/ && cd ../..
```

Or for a quick check during development, run it from within a package directory:

```bash
cd packages/partybox
uv run mypy src/
```

If mypy reports an error you don't understand, the [mypy documentation](https://mypy.readthedocs.io/en/stable/error_codes.html) explains every error code. The VS Code mypy extension surfaces errors inline as you type.

---

### pytest

[pytest](https://docs.pytest.org/) is the test runner. This project uses `pytest-asyncio` because most code is async (Bluetooth communication, the HTTP server, the service managers).

**Running tests:**

```bash
# Run all non-hardware tests for a package
uv run pytest packages/partybox/ -m "not hardware"

# Run a specific test file
uv run pytest packages/partybox/tests/unit/test_parser.py -v

# Run a specific test
uv run pytest packages/partybox/tests/unit/test_parser.py::test_power_response -v

# Run hardware tests (requires a real PartyBox)
uv run pytest packages/partybox/ -m hardware -v
```

**Hardware vs non-hardware tests:**

Tests that require a physical PartyBox are marked with `@pytest.mark.hardware`. They never run in CI. All other tests use `MockTransport` or byte fixtures and run everywhere.

The protocol tests are a good example of the project's testing philosophy: rather than constructing test packets from scratch or using mocked bytes, we use real Bluetooth captures — bytes that came off the wire from a real speaker. This means CI tests the actual protocol codec, not a fabricated approximation of it.

```python
# Byte fixture from a real capture — not fabricated
POWER_ON_RESPONSE = bytes.fromhex("aa550102000128")

def test_parse_power_on_response() -> None:
    msg = parse(POWER_ON_RESPONSE)
    assert isinstance(msg, PowerStateNotification)
    assert msg.state == PowerState.ON
```

---

### pre-commit

[pre-commit](https://pre-commit.com/) runs checks automatically before each `git commit`. In this project it runs Ruff format and lint checks.

It is installed as part of the dev container setup (`pre-commit install` wires it into the git hooks). If you set up your environment manually, run `pre-commit install` once after cloning.

**Why keep it enabled:**

pre-commit catches formatting and lint issues before they reach CI. A commit that would fail the CI format check is blocked locally, immediately, with a clear error message and an auto-fix. This is faster and less disruptive than discovering the failure after a push.

If a commit is blocked because Ruff found something: run `uv run ruff check --fix .`, stage the changes, and commit again. Most issues are auto-fixable.

---

### Dev container

The dev container ([.devcontainer/](../.devcontainer/devcontainer.json)) provides a complete, pre-configured development environment using VS Code and Docker. Every tool described in this guide — Python, uv, Ruff, mypy, pytest, pre-commit, the GitHub CLI, Claude Code — is installed and configured inside the container.

**Why it exists:**

Contributing to a project shouldn't require an afternoon of toolchain setup. The dev container makes the environment reproducible: the same Python version, the same tool versions, the same VS Code settings, on any machine.

**The base image is Debian Bookworm** — the same base as Raspberry Pi OS 64-bit. This means system library versions and package names are intentionally close to the production target. There's no guarantee that a package that compiles on macOS will compile on the Pi; starting from the same base reduces surprises.

**What the container intentionally does not do:**

The container does not emulate the appliance. It has no Bluetooth stack, no BlueZ, no D-Bus, no A2DP. The dev container is for developing and testing code; hardware integration tests require a native Linux environment or the Pi itself.

This is deliberate. Adding Bluetooth passthrough to a dev container is complex, fragile, and host-platform-specific. The `MockTransport` handles the development-time need; real hardware tests run on real hardware.

---

## Repository tour

```
partybox-companion/
├── packages/           Three Python packages (SDK, daemon, appliance)
├── docs/               All documentation — architecture, ADRs, protocol analysis
├── research/           Local RE workspace — gitignored, never committed
├── examples/           Standalone scripts using the partybox SDK directly
├── spike/              Exploratory code from viability spikes (M3 audio, etc.)
├── system/             systemd unit file and Avahi mDNS service record
└── webui/              Companion Portal frontend source (framework TBD)
```

**`packages/`** contains three Python packages with a strict one-way dependency: `partybox ← partyboxd ← companion`. Each is its own installable package with its own `pyproject.toml`. See [docs/architecture.md](architecture.md) for the module reference within each package.

**`docs/`** is the source of truth for project decisions. Architecture, ADRs, protocol analysis, model support, roadmap, validation results — if it's a decision or a discovery, it lives here.

**`research/`** is your local workspace for protocol analysis: raw APK files, JADX exports, Bluetooth captures, exploration scripts. This directory is gitignored entirely. Never commit its contents — not even accidentally. See [CONTRIBUTING.md](../CONTRIBUTING.md#legal-hygiene).

**`examples/`** contains standalone Python scripts that demonstrate using the `partybox` SDK. They run against a real speaker:

```bash
uv run python examples/scan.py
uv run python examples/connect.py
uv run python examples/power_on.py
```

**`spike/`** contains exploratory code from milestone viability spikes. `spike/m3-audio/` is the toolkit used to validate that a Pi can simultaneously stream A2DP audio and maintain a BLE control connection (see [M3 findings](validation/m3-findings.md)). Spike code is not production code — it exists to answer a question and is preserved so the answer can be understood.

**`system/`** contains the systemd unit file and Avahi mDNS service record used when the appliance runs on a Pi.

**`webui/`** will contain the Companion Portal frontend. Framework is TBD (M7).

---

## Common development tasks

### Adding a dependency

```bash
# Add a runtime dependency to a specific package
cd packages/partyboxd
uv add fastapi

# Add a dev dependency
uv add --dev httpx

# Sync the workspace after editing pyproject.toml manually
uv sync --all-extras
```

After adding a dependency, commit both `pyproject.toml` and `uv.lock`.

---

### Running tests

```bash
# Non-hardware tests for all packages
uv run pytest packages/partybox/  -m "not hardware"
uv run pytest packages/partyboxd/ -m "not hardware"
uv run pytest packages/companion/ -m "not hardware"

# Single test file
uv run pytest packages/partybox/tests/unit/test_parser.py -v

# Hardware tests (real PartyBox, optional BT address)
PARTYBOX_ADDRESS=AA:BB:CC:DD:EE:FF uv run pytest packages/partybox/ -m hardware -v
```

---

### Running examples

```bash
uv run python examples/scan.py         # discover nearby PartyBox speakers
uv run python examples/connect.py      # connect and print device info
uv run python examples/power_on.py     # turn the speaker on
```

These require Bluetooth access and a real PartyBox.

---

### Adding a protocol message

Protocol work follows a consistent sequence. The full walkthrough is in [Developer Handbook — Adding a protocol command](developer-handbook.md#adding-a-protocol-command) and [CONTRIBUTING.md](../CONTRIBUTING.md#adding-a-protocol-command).

The short version:

1. Find the opcode in the decompiled JBL app (`research/jadx-export/`) — see [docs/reverse-engineering/guide.md](reverse-engineering/guide.md)
2. Validate with a Bluetooth capture from nRF Connect
3. Document the finding in [docs/reverse-engineering/protocol.md](reverse-engineering/protocol.md)
4. Add the message dataclass → update parser/serializer/constants → expose via capability
5. Add a unit test using real capture bytes as a fixture

---

### Adding a capability

```
packages/partybox/src/partybox/device/capabilities/
├── power.py      ← example to follow
└── <name>.py     ← your new file
```

1. Create `capabilities/<name>.py` — a plain class following `power.py` (there is no shared base class)
2. Add the `@property` to `device/partybox.py` — typed `<Name>Capability | None` if the capability is optional
3. Export from `partybox/__init__.py` if it's part of the public API

The capability model is described in detail in [docs/architecture.md](architecture.md#capability-model) and [ADR-006](adr/006-capability-model.md).

---

### Updating documentation

Protocol documentation goes in `docs/reverse-engineering/`. Architecture decisions go in `docs/adr/`. General architecture goes in `docs/architecture.md`.

When you make a significant architectural decision — not a feature addition, but a decision about how the system is structured — write an ADR. See the [ADR README](adr/README.md) for format and existing examples.

---

### Writing an ADR

ADRs live in `docs/adr/`. Copy the format from an existing one (e.g., [ADR-006](adr/006-capability-model.md)): number, title, status, context, decision, and consequences. Number it sequentially.

The most important section is **Context** — what problem you were solving and what you considered. The decision itself is usually short. The consequences section is where you document what you accepted (trade-offs, future constraints) in order to make this decision.

---

## Why these tools

### Why uv?

`pip` + `virtualenv` + `pip-tools` is the traditional stack. It works, but it requires coordinating three tools with separate configuration files. `poetry` was a popular alternative but has its own diverging conventions. `uv` is faster than all of them, supports workspaces natively, and is compatible with standard `pyproject.toml` — no custom lock file format, no vendor lock-in.

For a monorepo with three inter-dependent packages, workspace support is the decisive feature. `uv sync --all-extras` installs everything in a single command.

### Why Ruff?

`black` + `isort` + `flake8` + plugins was the standard Python linter stack for years. Ruff is a single Rust-based tool that replaces all of them and runs in milliseconds rather than seconds. The configuration lives in one place (`pyproject.toml`). There is no meaningful downside.

### Why pyproject.toml?

`setup.py` and `setup.cfg` are the older formats. `pyproject.toml` is the current Python standard ([PEP 518](https://peps.python.org/pep-0518/), [PEP 621](https://peps.python.org/pep-0621/)). All tools in this project read from it — ruff, mypy, pytest, coverage, hatch. One file for everything.

### Why bleak?

`bleak` is the only cross-platform Python BLE library with active maintenance and good async support. On Linux it uses BlueZ via D-Bus; on macOS it uses CoreBluetooth. Both matter: development happens on macOS/Linux dev containers, production runs on a Pi (Linux).

The alternatives (`pygatt`, `bluepy`) are Linux-only or unmaintained. `bleak` is the obvious choice. See [ADR-015](adr/015-bluetooth-control-transport.md).

### Why hatchling?

`hatchling` is the build backend for each package. It reads all configuration from `pyproject.toml`, has no magic behaviour, and produces standard wheel and sdist distributions. We're not using any hatch-specific features beyond the build backend — it was chosen for being well-maintained and convention-free.

---

## Next steps

- **Architecture:** [docs/architecture.md](architecture.md) — detailed module reference and data flow diagrams
- **Roadmap:** [docs/roadmap.md](roadmap.md) — current milestone status and what's coming
- **Protocol analysis:** [docs/reverse-engineering/guide.md](reverse-engineering/guide.md) — how to analyse the Bluetooth protocol and contribute findings
- **Day-to-day commands:** [docs/developer-handbook.md](developer-handbook.md) — quick reference for common tasks
- **Contributing:** [CONTRIBUTING.md](../CONTRIBUTING.md) — contribution workflow, legal hygiene, PR process
