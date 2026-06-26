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

## Architecture

Four layers, strict one-way dependency:

```
partybox   (SDK, BLE GATT via bleak)
    ↑
partyboxd  (daemon: HTTP API + WebSocket)
    ↑
companion  (appliance: Portal, CLI, service orchestration)
    ↑
clients    (browsers, CLI, Home Assistant, scripts)
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

The SDK exposes only hardware-unique capabilities that Spotify Connect, AirPlay, and AVRCP cannot provide. Volume, play/pause, and skip are **not** in the SDK — librespot and shairport-sync handle those natively.

## Capability model

Capabilities are typed optional properties on `Device`. Callers check for `None`:

```python
await speaker.power.turn_on()        # always present
if speaker.battery is not None:      # optional — portable models only
    level = await speaker.battery.level()
```

Adding a capability: create `device/capabilities/<name>.py` implementing `Capability`, add optional `@property` to `device/base.py` and `device/partybox.py`.

## Testing approach

Protocol tests use **real Bluetooth captures as byte fixtures** — never fabricated bytes. This lets CI verify codec correctness without hardware:

```python
POWER_ON_RESPONSE = bytes.fromhex("aa550102000128")

def test_parse_power_on_response() -> None:
    msg = parse(POWER_ON_RESPONSE)
    assert isinstance(msg, PowerStateNotification)
```

`MockTransport` simulates the transport for all non-hardware tests. It can be configured to simulate connection drops and canned responses. Tests marked `@pytest.mark.hardware` never run in CI.

## Protocol work

When adding a new protocol command:
1. Locate opcode in JADX export of the JBL APK (`research/jadx-export/`) — see `docs/reverse-engineering/guide.md`
2. Validate with Bluetooth capture (`research/btsnoop/`)
3. Document in `docs/reverse-engineering/protocol.md`
4. Add message dataclass → update parser/serializer/constants → expose via capability
5. Add fixture-based unit test using real capture bytes

Document observations (what bytes appear on the wire). Do not transcribe or paraphrase proprietary source. Never commit APK files, JADX exports, or decompiled source — `research/` is gitignored for this reason.

## Commit messages

Conventional Commits with these scopes: `bluetooth`, `protocol`, `device`, `capabilities`, `api`, `cli`, `services`, `config`, `webui`, `docs`, `ci`
