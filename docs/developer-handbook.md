# Developer Handbook

Practical guide for working on partybox-companion.

For the why behind the architecture, see [architecture.md](architecture.md) and [docs/adr/](adr/).

---

## Prerequisites

- Linux or macOS (Linux required for hardware testing)
- Python 3.11+
- [uv](https://docs.astral.sh/uv/) — dependency and workspace manager
- A Bluetooth adapter (for hardware tests only)

```bash
# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## Setup

```bash
git clone https://github.com/jklingberg/partybox-companion
cd partybox-companion

# Install all packages + dev dependencies
uv sync --all-extras

# Install pre-commit hooks (lint + format run on every commit)
uv run pre-commit install
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
   - Capability unit test using `MockBackend`

8. **Expose via REST endpoint.** (from M7 onwards)

9. **Expose via CLI.** (from M6 onwards)

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
│   │       ├── bluetooth/ ← transport (BlueZBackend, MockBackend)
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
test(device): add MockBackend reconnect scenario
```

Scopes: `bluetooth`, `protocol`, `device`, `capabilities`, `api`, `cli`, `services`, `config`, `webui`, `docs`, `ci`
