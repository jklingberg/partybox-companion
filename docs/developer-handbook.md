# Developer Handbook

Practical guide for working on partybox-companion.

For the why behind the architecture, see [architecture.md](architecture.md) and [docs/adr/](adr/).

---

## Development environment

### Option A — Dev container (recommended)

The repository ships a [dev container](../.devcontainer/devcontainer.json) that provides a complete, pre-configured environment. No manual installation required.

**Prerequisites:** [VS Code](https://code.visualstudio.com/) and the [Dev Containers extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers).

```
1. Clone the repository
2. Open the folder in VS Code
3. When prompted, click "Reopen in Container"
   (or: Command Palette → "Dev Containers: Reopen in Container")
4. Wait ~2 minutes for the first build
```

The container runs `postCreateCommand` automatically on first open:

```bash
uv sync --all-extras       # install all workspace packages and dev deps
uv tool install pre-commit # install pre-commit as an isolated tool
pre-commit install         # wire up git hooks
npm install -g @anthropic-ai/claude-code
```

After that, the terminal is ready. `uv run pytest`, `uv run ruff`, and `uv run mypy` all work immediately.

**What the container includes:**

| Tool | Source |
|---|---|
| Python 3.12 | `mcr.microsoft.com/devcontainers/python:3.12-bookworm` |
| uv | `ghcr.io/astral-sh/uv:0.11.24` (pinned; copied at build time) |
| GitHub CLI (`gh`) | devcontainer feature |
| Node.js LTS | devcontainer feature (Claude Code dependency) |
| Claude Code | `npm install -g @anthropic-ai/claude-code` (post-create) |
| pre-commit | `uv tool install pre-commit` (post-create) |
| ruff, mypy, pytest | installed via `uv sync --all-extras` |

**Base image:** Debian 12 Bookworm — the same base as Raspberry Pi OS 64-bit. Package names and system library versions are intentionally close to the production target.

**What the container does not do:** Bluetooth passthrough, BlueZ, D-Bus, or A2DP. Hardware integration tests require a native Linux environment or the Pi itself. See [Hardware testing](#hardware-testing) below.

---

### Option B — Native setup

Use this when working on hardware integration, running tests against a real PartyBox, or if you prefer not to use Docker.

**Prerequisites:** Linux or macOS, Python 3.11+, [uv](https://docs.astral.sh/uv/).

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
# Format code
uv run ruff format .

# Lint
uv run ruff check .

# Fix auto-fixable lint issues
uv run ruff check --fix .

# Type check a specific package
uv run mypy src/   # run from the package directory, e.g. packages/partybox/

# Or type check all packages at once
for pkg in partybox partyboxd companion; do
  (cd packages/$pkg && uv run mypy src/)
done

# Run tests (excluding hardware)
uv run pytest packages/partybox/ -m "not hardware"
uv run pytest packages/partyboxd/ -m "not hardware"
uv run pytest packages/companion/ -m "not hardware"

# Run hardware tests (requires a real PartyBox connected over Bluetooth)
uv run pytest packages/partybox/ -m hardware
```

---

## Package boundaries

The dependency direction is strict: `partybox ← partyboxd ← companion`.

| Package | May import from | Must NOT import from |
|---|---|---|
| `partybox` | stdlib only | `partyboxd`, `companion` |
| `partyboxd` | `partybox`, stdlib, its own deps | `companion`, `partybox.bluetooth`, `partybox.protocol` directly |
| `companion` | `partyboxd`, `partybox` events/types | `partybox.bluetooth`, `partybox.protocol` directly |

`partyboxd` and `companion` consume the `Device` ABC and typed events. They never reach into Bluetooth or protocol internals.

---

## Adding a protocol command

Follow this sequence every time a new command is added:

1. **Find the opcode in the decompiled app.** Use JADX on the JBL APK (`research/jadx-export/`) to locate the relevant classes, constants, and payload structure. This gives you the opcode and field names before you write a byte of code. See `docs/reverse-engineering/guide.md`.

2. **Validate with a capture.** Use nRF Connect to capture HCI traffic while triggering the action in the JBL app. Confirm the bytes match what JADX suggested. Save the log to `research/btsnoop/`.

3. **Document the finding.** Add the opcode and payload layout to `docs/reverse-engineering/protocol.md`. Add it to `docs/reverse-engineering/discoveries.md`.

4. **Add the message type.**
   ```
   packages/partybox/src/partybox/protocol/messages.py
   ```
   Add a frozen dataclass for the command and/or response.

5. **Update the codec.**
   - `protocol/serializer.py` if it is an outbound command
   - `protocol/parser.py` if it is an inbound response or event
   - `protocol/constants.py` for the opcode constant

6. **Expose via a capability.**
   ```
   packages/partybox/src/partybox/device/capabilities/<name>.py
   ```
   Add the method to the relevant capability class. If it is a new capability type, also add the optional property to `device/base.py` and `device/partybox.py`.

7. **Add tests.**
   - Protocol unit test with the validated capture bytes as a fixture (so CI runs without hardware)
   - Capability unit test using `MockTransport`

8. **Expose via REST endpoint.** (from M8 onwards)

9. **Expose via CLI.** (from M8 onwards)

---

## Adding a new capability

New capabilities follow the same pattern as existing ones:

```
packages/partybox/src/partybox/device/capabilities/
├── base.py          ← Capability ABC — inherit from this
├── audio.py         ← example: see set_volume, set_mute
└── <your_name>.py   ← new file
```

1. Create `device/capabilities/<name>.py` implementing the `Capability` ABC
2. Add `@property def <name>(self) -> <Name>Capability | None` to `device/base.py`
3. Implement the property in `device/partybox.py` — return `None` if the connected device does not report support
4. Export from `partybox/__init__.py` if it should be part of the public API

---

## Hardware testing

Tests that require a real PartyBox are marked:

```python
@pytest.mark.hardware
def test_connect_to_real_device() -> None:
    ...
```

These are never run in CI. Run them locally when you have hardware:

```bash
uv run pytest packages/partybox/ -m hardware -v
```

Set the `PARTYBOX_ADDRESS` environment variable to the Bluetooth address of your device if the test requires it:

```bash
PARTYBOX_ADDRESS=AA:BB:CC:DD:EE:FF uv run pytest packages/partybox/ -m hardware
```

---

## Running the daemon locally

```bash
# Copy the example config
cp packages/companion/config/partybox-companion.example.toml /tmp/partyboxd.toml

# Edit the config — set your Bluetooth address
$EDITOR /tmp/partyboxd.toml

# Start the daemon
uv run --package partyboxd partyboxd --config /tmp/partyboxd.toml
```

The daemon starts at `http://localhost:8080`. Check `GET /api/v1/status`.

---

## Project structure reference

```
partybox-companion/
├── packages/
│   ├── partybox/          ← Bluetooth SDK
│   │   └── src/partybox/
│   │       ├── bluetooth/ ← transport (BleakTransport, MockTransport)
│   │       ├── protocol/  ← codec (parser, serializer, messages)
│   │       └── device/    ← Device ABC, capabilities, events
│   │
│   ├── partyboxd/         ← Headless daemon
│   │   └── src/partyboxd/
│   │       ├── api/       ← FastAPI app factory
│   │       ├── bus.py     ← internal event bus
│   │       └── config/    ← DaemonSettings (pydantic-settings)
│   │
│   └── companion/         ← Full appliance
│       └── src/companion/
│           ├── cli/       ← partybox CLI (HTTP client)
│           ├── services/  ← librespot + shairport-sync managers
│           └── webui/     ← static file serving
│
├── webui/                 ← Companion Portal source (framework TBD)
├── system/
│   ├── systemd/           ← systemd service file
│   └── avahi/             ← mDNS service record
├── docs/                  ← All documentation
└── research/              ← Local RE workspace (not in VCS)
```

---

## Commit messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(protocol): add EQ band command
fix(bluetooth): handle reconnect race condition
docs(protocol): document volume response format
test(device): add MockTransport reconnect scenario
```

Scopes: `bluetooth`, `protocol`, `device`, `capabilities`, `api`, `cli`, `services`, `config`, `webui`, `docs`, `ci`
