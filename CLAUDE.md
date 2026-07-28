# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

All commands run from the repository root unless noted.

```bash
# Install / sync dependencies
uv sync --all-extras

# Format
uv run ruff format .

# Lint (auto-fix)
uv run ruff check --fix .

# Type check (must run from each package directory)
cd packages/partybox  && uv run mypy src/ && cd ../..
cd packages/partyboxd && uv run mypy src/ && cd ../..
cd packages/companion && uv run mypy src/ && cd ../..

# Run all non-hardware tests
uv run pytest packages/partybox/  -m "not hardware"
uv run pytest packages/partyboxd/ -m "not hardware"
uv run pytest packages/companion/ -m "not hardware"

# Run a single test
uv run pytest packages/partybox/tests/unit/test_parser.py::test_power_response -v

# Run hardware tests (real PartyBox required; discovers by BLE name)
uv run pytest packages/partybox/ -m hardware -v
```

mypy is configured `strict` in the root `pyproject.toml`. All packages must pass `mypy --strict` — no exceptions.

New devcontainer terminals do **not** auto-activate `.venv` (`python.terminal.activateEnvironment` is disabled in `.devcontainer/devcontainer.json`) — always prefix commands with `uv run`, as shown above, rather than assuming an activated shell.

## Architecture

Four layers, strict one-way dependency:

```
partybox   (SDK, BLE GATT via bleak)
    ↑
partyboxd  (daemon: HTTP API + WebSocket)
    ↑
companion  (appliance: Portal, service orchestration)
    ↑
clients    (browsers, Home Assistant, scripts)
```

`companion` extends `partyboxd`'s FastAPI app **in-process** — same port, same process, no IPC:

```python
# companion/src/companion/__main__.py
app = create_daemon_app(settings.daemon)   # from partyboxd
app.mount("/", webui_router)               # Companion Portal
app.include_router(services_router, ...)   # librespot + shairport-sync
```

Running `partyboxd` gives the headless API. Running `partybox-companion` gives the full appliance with Portal and streaming services.

## SDK boundaries

`partybox` depends only on **`bleak`** (BLE GATT transport — see [ADR-015](docs/adr/015-bluetooth-control-transport.md)). It must never contain:
- Networking beyond Bluetooth (no HTTP, WebSockets)
- Subprocess management
- Configuration loading
- Knowledge of the daemon, REST API, Portal, Spotify, or AirPlay

Speaker control is **BLE GATT**, not Bluetooth Classic SPP/RFCOMM (an earlier assumption, since disproven on hardware). Commands are written to a vendor GATT characteristic; responses arrive as notifications. Bluetooth Classic carries only A2DP audio and AVRCP.

The SDK exposes only hardware-unique capabilities that Spotify Connect, AirPlay, and AVRCP cannot provide. Play/pause and skip are **not** in the SDK — librespot and shairport-sync handle those natively. Hardware volume is the one exception: `VolumeCapability` exists per the volume authority model ([ADR-022](docs/adr/022-volume-authority.md)), but its BLE opcode is not yet confirmed and both methods raise `NotImplementedError`.

## Capability model

Capabilities are typed properties on `PartyBoxDevice` — plain classes, no shared base. Optional capabilities are `None` when unsupported; callers check for `None`:

```python
await speaker.power.turn_on()        # always present
if speaker.battery is not None:      # optional — portable models only
    level = await speaker.battery.level()
```

Adding a capability: create `device/capabilities/<name>.py` (follow `power.py` as the template), add a `@property` to `device/partybox.py` (typed `<Name>Capability | None` if optional), and export it from `partybox/__init__.py` if public.

## Testing approach

Protocol tests use **real Bluetooth captures as byte fixtures** — never fabricated bytes. This lets CI verify codec correctness without hardware:

```python
POWER_ON_RESPONSE = bytes.fromhex("aa550102000128")

def test_parse_power_on_response() -> None:
    msg = parse(POWER_ON_RESPONSE)
    assert isinstance(msg, PowerStateNotification)
```

`MockTransport` simulates the transport for all non-hardware tests. It can be configured to simulate connection drops and canned responses. Tests marked `@pytest.mark.hardware` never run in CI.

Adding a new BLE protocol command or working on the appliance Raspberry Pi (SSH access, deploying source, service/log commands, restarts)? See the `add-protocol-command` and `pi-hardware-ops` skills.

## Commit messages

Conventional Commits with these scopes: `bluetooth`, `protocol`, `device`, `capabilities`, `api`, `services`, `config`, `webui`, `docs`, `ci`
