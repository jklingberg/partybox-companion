# Architecture

> **See also:**
> [Vision](vision.md) · [Developer Handbook](developer-handbook.md) · [Roadmap](roadmap.md) · [Model Support](model-support.md) · [ADRs](adr/)

---

## Overview

partybox-companion is an appliance. It turns a JBL PartyBox into a smart WiFi speaker using a Raspberry Pi. The architecture is designed around that goal: simple to install, stable to run, and extensible without modifying the core.

Four layers, each independently useful:

```
partybox           Bluetooth SDK. BLE GATT via bleak.
    ↑
partyboxd          Headless daemon. HTTP API + WebSocket.
    ↑
companion          Full appliance. Companion Portal, CLI, service orchestration.
    ↑
clients            Browsers, CLI, Home Assistant, scripts.
```

Each layer depends only on the layer below. No layer has knowledge of the layer above it. The key design rationale for each decision is documented in [docs/adr/](adr/).

---

## Repository structure

```
partybox-companion/
├── packages/
│   ├── partybox/                          ← Python package: partybox (SDK)
│   │   └── src/partybox/
│   │       ├── bluetooth/                 ← transport abstraction
│   │       ├── protocol/                  ← binary codec (stateless, pure)
│   │       └── device/                    ← device model, capabilities, events
│   │           └── capabilities/          ← one file per capability type
│   │
│   ├── partyboxd/                         ← Python package: partyboxd (daemon)
│   │   └── src/partyboxd/
│   │       ├── api/                       ← FastAPI app factory
│   │       ├── bus.py                     ← internal event bus
│   │       └── config/                    ← DaemonSettings (pydantic-settings)
│   │
│   └── companion/                         ← Python package: partybox-companion
│       └── src/companion/
│           ├── cli/                       ← partybox CLI (thin HTTP client)
│           ├── services/                  ← librespot + shairport-sync managers
│           └── webui/                     ← static file serving
│
├── webui/                                 ← Companion Portal source (framework TBD)
├── system/
│   ├── systemd/partyboxd.service          ← systemd unit file
│   └── avahi/partyboxd.service            ← mDNS record
├── docs/                                  ← All documentation
└── research/                              ← Local RE workspace (not in VCS)
```

---

## Package responsibilities

### `partybox` — Bluetooth SDK

The most reusable part of the project. A standalone Python library for communicating with PartyBox speakers.

Speaker control runs over **BLE GATT**, using `bleak` as the transport (see [ADR-015](adr/015-bluetooth-control-transport.md)). `bleak` is the only runtime dependency.

Published independently on PyPI so that developers can build tools on top of the protocol without installing the daemon.

**Must never contain:** networking beyond Bluetooth, subprocess management, daemon lifecycle, configuration loading, or knowledge of REST/Companion Portal/Spotify/AirPlay.

```python
from partybox import PartyBox

speaker = await PartyBox.discover()
await speaker.power.turn_on()
await speaker.audio.set_volume(40)

if speaker.battery is not None:
    level = await speaker.battery.level()
```

### `partyboxd` — Headless daemon

Consumes `partybox`. Runs continuously as a system service. Exposes a stable HTTP API.

Useful standalone for power users who want the API without the full appliance — scripting, direct HA integration, custom UIs, minimal installs.

Binary: `partyboxd`

### `companion` — Full appliance

Consumes `partyboxd`. This is what most users install.

Extends `partyboxd`'s FastAPI application in-process via the `create_app()` factory. Single process, single port.

Binaries: `partybox-companion` (starts everything), `partybox` (CLI client)

---

## `partybox` SDK — module reference

### `bluetooth/`

| File | Purpose |
|---|---|
| `transport.py` | `ControlTransport` ABC: `connect`, `disconnect`, `write`, `receive` |
| `bleak_transport.py` | `BleakTransport` — BLE GATT client via `bleak`; control service write + notify |
| `mock.py` | `MockTransport` — in-process fake; simulates drops, errors, canned responses |
| `scanner.py` | `Scanner` — discover PartyBoxes over BLE, returning `PartyBoxCandidate` |

`BleakTransport` and `MockTransport` are never imported outside `bluetooth/` and test fixtures. Callers depend only on `ControlTransport` and the domain types (`Scanner`, `PartyBoxCandidate`) — no `bleak` type is exposed. Discovery hides the speaker's rotating BLE address: a `PartyBoxCandidate` carries the live device handle and `await candidate.connect()` returns a connected `ControlTransport`.

The control transport is message-oriented: `write(data)` sends a command frame to the TX characteristic; `receive()` returns the next notification payload from the RX characteristic.

### `protocol/`

Stateless. Pure functions. No I/O.

| File | Purpose |
|---|---|
| `frame.py` | `Frame` dataclass: header, opcode, payload, checksum |
| `messages.py` | All message dataclasses (commands + notifications) |
| `parser.py` | `parse(bytes) -> Message` |
| `serializer.py` | `serialize(Message) -> bytes` |
| `constants.py` | Protocol byte constants |

### `device/`

| File | Purpose |
|---|---|
| `base.py` | `Device` ABC with capability properties |
| `partybox.py` | `PartyBoxDevice` — wires transport + protocol; capability registry; event stream |
| `state.py` | `DeviceState` frozen dataclass |
| `types.py` | Domain enums: `InputSource`, `PowerState`, `SoundMode`, etc. |

#### Capability model

PartyBox models differ in what they support. Capabilities are typed optional properties on `Device`; callers check for `None` to determine support. See [ADR-006](adr/006-capability-model.md) and [ADR-010](adr/010-sdk-scope.md).

**Scope rule:** the SDK exposes only hardware-unique capabilities that open protocols (Spotify Connect, AirPlay, AVRCP) cannot provide. Volume, play/pause, and skip are not in the SDK. See [model-support.md](model-support.md).

```
device/capabilities/
├── __init__.py
├── base.py           ← Capability ABC
├── power.py          ← on/off, power state        (required — always present)
├── device_info.py    ← firmware version, model    (required — always present)
├── battery.py        ← level, charging status     (optional — portable models)
├── lights.py         ← lighting modes, colours    (optional — post-v1.0)
├── microphone.py     ← mute, karaoke features     (optional — post-v1.0)
└── eq.py             ← EQ bands, sound mode presets (optional — post-v1.0)
```

```python
class Device:
    # MVP — required on every device
    @property
    def power(self) -> PowerCapability: ...
    @property
    def device_info(self) -> DeviceInfoCapability: ...

    # Optional — None if unsupported
    @property
    def battery(self) -> BatteryCapability | None: ...

    # Post-v1.0
    @property
    def lights(self) -> LightsCapability | None: ...
    @property
    def microphone(self) -> MicrophoneCapability | None: ...
    @property
    def eq(self) -> EQCapability | None: ...

    def events(self) -> AsyncIterator[DeviceEvent]: ...
```

#### Event stream

`PartyBoxDevice` yields typed domain events as an async generator. There is no embedded event bus in the SDK — that is `partyboxd`'s concern.

```python
async for event in device.events():
    ...  # daemon wires this into its internal bus
```

---

## `partyboxd` — module reference

### `api/`

FastAPI application factory (`create_app(settings) -> FastAPI`).

- REST API at `/api/v1/`
- WebSocket event stream at `/ws`
- API key auth applied globally via FastAPI dependency

### `bus.py`

Internal async event bus. Subscribes to `device.events()` and re-emits to registered handlers (the API layer, service managers).

### `config/`

`DaemonSettings` via pydantic-settings. Reads from a TOML file and environment variables.

---

## `companion` — module reference

### `cli/`

`partybox` CLI (Typer + Rich). Every command is an HTTP request to `partyboxd`. Connects to `localhost:8080` by default; override with `--url` or `PARTYBOX_URL`.

### `services/`

| File | Purpose |
|---|---|
| `backend.py` | `ServiceBackend` ABC: `start`, `stop`, `is_running` |
| `librespot.py` | Spotify Connect via librespot subprocess |
| `shairport.py` | AirPlay via shairport-sync subprocess |
| `manager.py` | Subscribes to daemon events; coordinates service lifecycle |

### `webui/`

Serves the Companion Portal static files at `/`.

---

## Runtime model

`companion` extends `partyboxd`'s FastAPI app in-process:

```python
# partyboxd
def create_app(settings: DaemonSettings) -> FastAPI: ...

# companion
from partyboxd.api import create_app as create_daemon_app

def create_companion_app(settings: CompanionSettings) -> FastAPI:
    app = create_daemon_app(settings.daemon)
    app.mount("/", webui_router)
    app.include_router(services_router, prefix="/api/v1/services")
    return app
```

Running `partyboxd` gives you the headless API. Running `partybox-companion` gives you the full appliance. Same routes, same port — companion adds to it. See [ADR-005](adr/005-appliance.md).

---

## Data flow

### State changes (outbound)

```
ControlTransport → protocol.parser → PartyBoxDevice
    │ device.events() async generator
    ▼
partyboxd event bus
    ├──► api/ WebSocket → connected browsers / partybox watch
    └──► companion services manager → reacts to power-off, etc.
```

### Commands (inbound)

```
HTTP client (Companion Portal / partybox CLI / HA / script)
    │ POST /api/v1/power/on
    ▼
partyboxd route handler
    │ await device.power.turn_on()
    ▼
PartyBoxDevice → protocol.serializer → ControlTransport → speaker
```

---

## Communication

Single HTTP server on one port — 8080 by default in code, 80 in production (see [ADR-017](adr/017-runtime-layout.md)). See [ADR-007](adr/007-tcp-only.md).

| Path | Content |
|---|---|
| `/` | Companion Portal static files |
| `/api/v1/` | REST API |
| `/ws` | WebSocket event stream |

mDNS via system `avahi-daemon` → `http://partybox.local`.

API key auth via `X-API-Key` header. Generate with `partybox generate-key`. Optional on trusted networks.

---

## Testing strategy

| Package | Module | Approach |
|---|---|---|
| `partybox` | `protocol/` | Pure unit tests; real BT captures as byte fixtures |
| `partybox` | `bluetooth/` | `MockTransport` with simulated drops and errors |
| `partybox` | `device/` | `MockTransport`; assert state and yielded events |
| `partybox` | `capabilities/` | Unit tests per capability; mock device responses |
| `partyboxd` | `api/` | FastAPI async test client (httpx); mock `Device` |
| `companion` | `cli/` | Typer `CliRunner`; mock HTTP responses |
| `companion` | `services/` | Mock `asyncio.create_subprocess_exec` |

Hardware tests (`@pytest.mark.hardware`) require a real PartyBox. Never run in CI.
