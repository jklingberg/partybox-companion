# Developer Guide

> **See also:**
> [Architecture](architecture.md) · [Roadmap](roadmap.md) · [ADRs](adr/) · [Contributing](../CONTRIBUTING.md)

Welcome to partybox-companion. This is the single guide for working on the
project — from first clone to deploying a change on the Pi. It is written for
experienced developers who may not have used modern Python tooling before; if
you're coming from Go, Rust, Java, or C#, the concepts are familiar but the
tools differ.

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

---

## Development environment

### Option A — Dev container (recommended)

The repository ships a [dev container](../.devcontainer/devcontainer.json) that provides a complete, pre-configured environment. Open the repository in VS Code, accept the prompt to reopen in a container, and wait about two minutes for the first build. After that, `uv run pytest` and every other command in this guide work immediately.

**Prerequisites:** [VS Code](https://code.visualstudio.com/) and the [Dev Containers extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers).

The container runs `postCreateCommand` automatically on first open:

```bash
uv sync --all-extras       # install all workspace packages and dev deps
uv tool install pre-commit # install pre-commit as an isolated tool
pre-commit install         # wire up git hooks
npm install -g @anthropic-ai/claude-code
```

**What the container includes:**

| Tool | Source |
|---|---|
| Python 3.14 | `mcr.microsoft.com/devcontainers/python:3.14-bookworm` |
| uv | `ghcr.io/astral-sh/uv:0.11.24` (pinned; copied at build time) |
| GitHub CLI (`gh`) | devcontainer feature |
| Node.js LTS | devcontainer feature (Claude Code dependency) |
| Claude Code | `npm install -g @anthropic-ai/claude-code` (post-create) |
| pre-commit | `uv tool install pre-commit` (post-create) |
| ruff, mypy, pytest | installed via `uv sync --all-extras` |

**Base image:** Debian 12 Bookworm — the same base as Raspberry Pi OS 64-bit. Package names and system library versions are intentionally close to the production target, which reduces "works on my machine" surprises when deploying.

**What the container intentionally does not do:** Bluetooth passthrough, BlueZ, D-Bus, or A2DP. Adding those to a container is fragile and host-platform-specific. `MockTransport` handles the development-time need; hardware integration tests require a native Linux environment or the Pi itself (see [Hardware testing](#hardware-testing)).

### Option B — Native setup

Use this when working on hardware integration, running tests against a real PartyBox, or if you prefer not to use Docker.

**Prerequisites:** Linux or macOS, Python 3.14, [uv](https://docs.astral.sh/uv/).

```bash
# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and install
git clone https://github.com/jklingberg/partybox-companion
cd partybox-companion
uv sync --all-extras

# Install pre-commit hooks
uv tool install pre-commit
pre-commit install
```

---

## Day-to-day commands

All commands run from the repository root.

```bash
# Install / sync the workspace (after cloning, or after changing dependencies)
uv sync --all-extras

# Format
uv run ruff format .

# Lint (and auto-fix)
uv run ruff check .
uv run ruff check --fix .

# Type check all packages (mypy runs per-package because each has its own src/)
for pkg in partybox partyboxd companion; do
  (cd packages/$pkg && uv run mypy src/)
done

# Run non-hardware tests
uv run pytest packages/partybox/  -m "not hardware"
uv run pytest packages/partyboxd/ -m "not hardware"
uv run pytest packages/companion/ -m "not hardware"

# Run a single test
uv run pytest packages/partybox/tests/unit/test_codec.py::test_parse_power_on_response -v

# Run hardware tests (requires a real PartyBox over Bluetooth)
uv run pytest packages/partybox/ -m hardware -v
```

> The three test suites are run separately: each package has its own `tests/` package, so a single combined `pytest` invocation collides on the duplicate `tests` module name.

---

## The toolchain

If you're new to modern Python tooling, this is the short version of what each tool does and why it's here. All configuration lives in [pyproject.toml](../pyproject.toml).

### uv

[uv](https://docs.astral.sh/uv/) is the package manager and virtual-environment tool — think npm/Cargo plus the virtualenv plus the lockfile, in one fast tool. The decisive feature for this repo is **workspace support**: uv manages all three packages (`partybox`, `partyboxd`, `companion`) as a single workspace with a shared lockfile (`uv.lock`), so package versions never drift apart. `uv run <cmd>` runs a command in the environment without activating it; the venv lives at `.venv/` in the repo root.

### Ruff

[Ruff](https://docs.astral.sh/ruff/) handles both formatting and linting, replacing `black` + `isort` + `flake8` + plugins with one fast tool and one config block. It runs on save in the dev container and as a pre-commit hook. Notable enabled rule sets: `ANN` (annotations required on public functions — pairs with mypy), `S` (flake8-bandit security checks), `B` (flake8-bugbear bug patterns).

### mypy

[mypy](https://mypy.readthedocs.io/) is the static type checker, run in `strict` mode: every signature annotated, explicit return types, `Any` acknowledged, optionals checked before use. Type errors in the protocol layer (binary data) and the device layer (typed capabilities exposed to consumers) are real bugs; strict mode catches them at development time rather than at runtime on a Pi. It matters doubly for the SDK — `partybox` is published, so a typed public API is a real deliverable.

### pytest

[pytest](https://docs.pytest.org/) (with `pytest-asyncio`, since most code is async) is the test runner. Tests requiring a physical PartyBox are marked `@pytest.mark.hardware` and never run in CI; everything else uses `MockTransport` or byte fixtures. Protocol tests use **real Bluetooth captures as byte fixtures** rather than fabricated bytes, so CI exercises the actual codec:

```python
# Byte fixture from a real capture — not fabricated
POWER_ON_RESPONSE = bytes.fromhex("aa550102000128")

def test_parse_power_on_response() -> None:
    msg = decode(POWER_ON_RESPONSE)
    assert isinstance(msg, PowerStateNotification)
    assert msg.state == PowerState.ON
```

### pre-commit

[pre-commit](https://pre-commit.com/) runs Ruff (format + lint) and mypy before each commit, catching issues locally that would otherwise fail CI. If a commit is blocked, run `uv run ruff check --fix .`, stage, and commit again. `pre-commit install` (done automatically in the dev container) wires it into the git hooks.

---

## Package boundaries

The dependency direction is strict: `partybox ← partyboxd ← companion`.

| Package | May import from | Must NOT import from |
|---|---|---|
| `partybox` | stdlib + `bleak` | `partyboxd`, `companion` |
| `partyboxd` | `partybox`, stdlib, its own deps | `companion`, `partybox.bluetooth`, `partybox.protocol` directly |
| `companion` | `partyboxd`, `partybox` events/types | `partybox.bluetooth`, `partybox.protocol` directly |

`partyboxd` and `companion` consume `PartyBoxDevice` and typed events. They never reach into Bluetooth or protocol internals.

---

## Repository tour

```
partybox-companion/
├── packages/
│   ├── partybox/          ← Bluetooth SDK
│   │   └── src/partybox/
│   │       ├── bluetooth/ ← transport (BleakTransport, MockTransport) + scanner
│   │       ├── protocol/  ← binary codec (codec.py, messages.py, constants.py)
│   │       └── device/    ← PartyBoxDevice + capabilities
│   │
│   ├── partyboxd/         ← Headless daemon
│   │   └── src/partyboxd/
│   │       ├── api/       ← FastAPI app factory, routes, WebSocket, auth
│   │       ├── device/    ← DeviceManager + event bus
│   │       └── config/    ← Settings (pydantic-settings)
│   │
│   └── companion/         ← Full appliance
│       └── src/companion/
│           ├── services/  ← audio, pairing, Spotify, provisioning, BlueZ
│           ├── webui/     ← Companion Portal (static HTML + config API)
│           └── wifi/      ← provisioning API + captive-portal middleware
│
├── examples/             ← Standalone scripts using the partybox SDK directly
├── image/                ← Pi image build (install.sh + configs)
├── system/               ← systemd unit + Avahi mDNS service record
├── docs/                 ← All documentation (architecture, ADRs, protocol, roadmap)
└── research/             ← Local RE workspace — gitignored, never committed
```

**`packages/`** contains three Python packages with a strict one-way dependency: `partybox ← partyboxd ← companion`. Each is its own installable package with its own `pyproject.toml`. See [docs/architecture.md](architecture.md) for the module reference within each package.

**`docs/`** is the source of truth for project decisions. Architecture, ADRs, protocol analysis, model support, roadmap, validation results — if it's a decision or a discovery, it lives here.

**`research/`** is your local workspace for protocol analysis: raw APK files, JADX exports, Bluetooth captures, exploration scripts. This directory is gitignored entirely. Never commit its contents — not even accidentally. See [CONTRIBUTING.md](../CONTRIBUTING.md#legal-hygiene).

**`examples/`** contains standalone scripts that demonstrate using the `partybox` SDK against a real speaker:

```bash
uv run python examples/scan.py         # discover nearby PartyBox speakers
uv run python examples/connect.py      # connect and print device info
uv run python examples/power_on.py     # turn the speaker on
```

---

## Adding a protocol command

Follow this sequence every time a new command is added:

1. **Find the opcode in the decompiled app.** Use JADX on the JBL APK (`research/jadx-export/`) to locate the relevant classes, constants, and payload structure. This gives you the opcode and field names before you write a byte of code. See `docs/reverse-engineering/guide.md`.

2. **Validate with a capture.** Use nRF Connect to capture HCI traffic while triggering the action in the JBL app. Confirm the bytes match what JADX suggested. Save the log to `research/btsnoop/`.

3. **Document the finding.** Add the opcode and payload layout to `docs/reverse-engineering/protocol.md`, and record it in `docs/reverse-engineering/discoveries.md`.

4. **Add the message type.** Add a frozen dataclass for the command and/or response in `protocol/messages.py`, and its opcode constant in `protocol/constants.py`.

5. **Update the codec.** Extend `protocol/codec.py` (`encode` for outbound commands, `decode` for inbound responses/events).

6. **Expose via a capability.** Add the method to the relevant capability class in `device/capabilities/<name>.py`. If it is a new capability type, also add the property to `device/partybox.py` (typed `<Name>Capability | None` if optional).

7. **Add tests.** A protocol unit test with the validated capture bytes as a fixture (so CI runs without hardware), and a capability unit test using `MockTransport`.

8. **Expose via REST endpoint** and, where relevant, the Companion Portal.

---

## Adding a new capability

```
packages/partybox/src/partybox/device/capabilities/
├── power.py         ← example to follow
├── battery.py       ← example of an optional capability
└── <your_name>.py   ← new file
```

1. Create `device/capabilities/<name>.py` — a plain class following `power.py` (there is no shared base class)
2. Add `@property def <name>(self) -> <Name>Capability | None` to `device/partybox.py` — return `None` if the connected device does not report support (required capabilities are non-optional)
3. Export from `partybox/__init__.py` if it should be part of the public API

The capability model is described in detail in [docs/architecture.md](architecture.md#capability-model) and [ADR-006](adr/006-capability-model.md).

---

## Hardware testing

Tests that require a real PartyBox are marked `@pytest.mark.hardware` and never run in CI. Run them locally when you have hardware:

```bash
uv run pytest packages/partybox/ -m hardware -v

# Set the device address if the test requires it:
PARTYBOX_ADDRESS=AA:BB:CC:DD:EE:FF uv run pytest packages/partybox/ -m hardware
```

---

## Running the appliance locally

```bash
# Start the full companion appliance (REST API + Portal + Spotify service)
uv run partybox-companion

# Override port or other settings
COMPANION_PORT=9000 uv run partybox-companion
COMPANION_LOG_LEVEL=DEBUG uv run partybox-companion
```

The Portal and API start at `http://localhost:8080` in dev (the code default; production binds port 80 — see [ADR-017](adr/017-runtime-layout.md)). Check liveness with `GET /api/v1/health`.

To develop Portal UI without hardware, open `http://localhost:8080/?mock` — mock mode serves the Portal with simulated device state.

---

## Deploying to the Pi

The Pi runs the full `partybox-companion` appliance. The appliance venv at
`/opt/partybox-companion/` is a `--no-editable` install — source is copied into
site-packages — so for Python-only changes, rsync the source directly and restart
the service. No reinstall is required.

**SSH access:** `ssh pi@partybox.local` (mDNS, preferred) or `ssh pi@partybox` (router DNS). See [CLAUDE.md](../CLAUDE.md) for the credential and `sshpass` details used in this project.

**Deploy changed source files:**

Site-packages is root-owned on release images, so the remote rsync runs under sudo (`--rsync-path="sudo rsync"`).

```bash
# From the repo root — companion package shown; same pattern for partyboxd / partybox
rsync -a --delete --exclude='__pycache__' --rsync-path="sudo rsync" \
    packages/companion/src/companion/ \
    pi@partybox.local:/opt/partybox-companion/lib/python3.14/site-packages/companion/
```

**If dependencies changed** (`pyproject.toml` or `uv.lock`) or any `install.sh`-managed file (systemd unit, BlueZ config, Avahi record), a full image rebuild and reflash is required — the venv is built at image creation time and cannot be updated in-place without `uv`.

**Restart, health check, and logs:**

```bash
ssh pi@partybox.local "sudo systemctl restart companion"
ssh pi@partybox.local "curl -s http://localhost/api/v1/health"
ssh pi@partybox.local "journalctl -u companion -f"
ssh pi@partybox.local "journalctl -u companion -n 100 --no-pager"
```

**Bluetooth adapter wedge (scanner works, GATT connections fail):** the Pi's controller can enter a state where BLE scanning succeeds but every GATT connection fails. Restarting the Bluetooth stack recovers it without touching the speaker; power-cycle the speaker only as a last resort.

```bash
ssh pi@partybox.local "sudo systemctl restart bluetooth"
```

---

## Documentation and ADRs

Protocol documentation goes in `docs/reverse-engineering/`. Architecture decisions go in `docs/adr/`. General architecture goes in `docs/architecture.md`.

When you make a significant architectural decision — not a feature addition, but a decision about how the system is structured — write an ADR. Copy the format from an existing one (e.g. [ADR-006](adr/006-capability-model.md)): number, title, status, context, decision, consequences. The most important section is **Context** — what problem you were solving and what you considered. See the [ADR README](adr/README.md).

---

## Commit messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(protocol): add EQ band command
fix(bluetooth): handle reconnect race condition
docs(protocol): document volume response format
test(device): add MockTransport reconnect scenario
```

Scopes: `bluetooth`, `protocol`, `device`, `capabilities`, `api`, `services`, `config`, `webui`, `docs`, `ci`

---

## Next steps

- **Architecture:** [docs/architecture.md](architecture.md) — module reference and data flow diagrams
- **Roadmap:** [docs/roadmap.md](roadmap.md) — current milestone status and what's deferred past v1.0
- **Protocol analysis:** [docs/reverse-engineering/guide.md](reverse-engineering/guide.md) — how to analyse the Bluetooth protocol and contribute findings
- **Contributing:** [CONTRIBUTING.md](../CONTRIBUTING.md) — contribution workflow, legal hygiene, PR process
