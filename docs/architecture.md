# Architecture

> **See also:**
> [Vision](vision.md) · [Developer Guide](developer-guide.md) · [Roadmap](roadmap.md) · [Model Support](model-support.md) · [ADRs](adr/)

---

## Overview

partybox-companion is an appliance. It turns a JBL PartyBox into a smart WiFi speaker using a Raspberry Pi. The architecture is designed around that goal: simple to install, stable to run, and extensible without modifying the core.

Four layers, each independently useful:

```
partybox           Bluetooth SDK. BLE GATT via bleak.
    ↑
partyboxd          Headless daemon. HTTP API + WebSocket.
    ↑
companion          Full appliance. Companion Portal, service orchestration.
    ↑
clients            Browsers, Home Assistant, scripts.
```

Each layer depends only on the layer below. No layer has knowledge of the layer above it. The key design rationale for each decision is documented in [docs/adr/](adr/).

---

## Repository structure

```
partybox-companion/
├── packages/
│   ├── partybox/                          ← Python package: partybox (SDK)
│   │   └── src/partybox/
│   │       ├── bluetooth/                 ← transport abstraction + BLE scanner
│   │       ├── protocol/                  ← binary codec (stateless, pure)
│   │       └── device/                    ← PartyBoxDevice + capabilities
│   │           └── capabilities/          ← one file per capability type
│   │
│   ├── partyboxd/                         ← Python package: partyboxd (daemon)
│   │   └── src/partyboxd/
│   │       ├── api/                       ← FastAPI app factory, routes, WebSocket, auth
│   │       ├── device/                    ← DeviceManager + event bus
│   │       └── config/                    ← Settings (pydantic-settings)
│   │
│   └── companion/                         ← Python package: partybox-companion
│       └── src/companion/
│           ├── services/                  ← audio, pairing, Spotify, provisioning, BlueZ
│           ├── webui/                     ← Companion Portal (static HTML + config API)
│           └── wifi/                      ← provisioning API + captive-portal middleware
│
├── system/
│   ├── systemd/companion.service          ← systemd unit file
│   └── avahi/partyboxd.service            ← mDNS record
├── image/                                 ← Pi image build (install.sh + configs)
├── examples/                              ← SDK and API usage examples
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
from partybox import Scanner

speaker = await Scanner.find()
async with speaker:
    await speaker.power.turn_on()

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

Binary: `partybox-companion` (starts everything).

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
| `messages.py` | All message dataclasses (commands + notifications) |
| `codec.py` | `decode(bytes) -> Message` and `encode(Message) -> bytes` |
| `constants.py` | Protocol byte constants (opcodes, framing) |

### `device/`

| File | Purpose |
|---|---|
| `partybox.py` | `PartyBoxDevice` — wires transport + protocol; owns the capability properties |
| `capabilities/` | One plain class per capability (no shared base) |

#### Capability model

PartyBox models differ in what they support. Capabilities are typed properties on `PartyBoxDevice`; optional ones are `None` when unsupported, and callers check for `None` to determine support. See [ADR-006](adr/006-capability-model.md) and [ADR-010](adr/010-sdk-scope.md).

**Scope rule:** the SDK exposes only hardware-unique capabilities that open protocols (Spotify Connect, AirPlay, AVRCP) cannot provide. Play/pause and skip are not in the SDK. Hardware volume is the one exception — `VolumeCapability` exists per the volume authority model ([ADR-022](adr/022-volume-authority.md)), but its BLE opcode is not yet confirmed and both methods raise `NotImplementedError`. See [model-support.md](model-support.md).

```
device/capabilities/
├── __init__.py
├── power.py          ← on/off, power state        (required — always present)
├── device_info.py    ← firmware version, model    (required — always present)
├── battery.py        ← level, charging status     (optional — portable models)
└── volume.py         ← hardware volume            (BLE opcode not yet confirmed)
```

Planned post-v1.0 capabilities (lights, microphone, EQ) follow the same one-file-per-capability pattern.

```python
class PartyBoxDevice:
    # Required on every device
    @property
    def power(self) -> PowerCapability: ...
    @property
    def device_info(self) -> DeviceInfoCapability: ...

    # Optional — None if unsupported (detected at connect time
    # via the BLE battery service UUID)
    @property
    def battery(self) -> BatteryCapability | None: ...

    # Present when connected; methods raise NotImplementedError
    # until the BLE opcode is confirmed (ADR-022)
    @property
    def volume(self) -> VolumeCapability: ...
```

#### Event stream

The SDK does not yet expose device events. Unsolicited BLE notifications are currently drained and discarded inside `PartyBoxDevice`; a typed async event iterable is planned. The daemon-level event bus (`partyboxd.device.events.EventBus`) emits connection and command events from `DeviceManager` — that is `partyboxd`'s concern, not the SDK's.

---

## `partyboxd` — module reference

### `api/`

FastAPI application factory (`create_app(settings, ...) -> FastAPI` in `app.py`).

| File | Purpose |
|---|---|
| `app.py` | `create_app()` — assembles routes, WebSocket, auth |
| `routes.py` | REST endpoints at `/api/v1/` |
| `ws.py` | WebSocket event stream at `/api/v1/events` |
| `auth.py` | `X-Api-Key` auth dependency — applied to all routes except `GET /api/v1/health` |

### `device/`

| File | Purpose |
|---|---|
| `manager.py` | `DeviceManager` — owns the BLE connection lifecycle (scan, connect, reconnect, health probe) and exposes a `StatusSnapshot` |
| `events.py` | Event dataclasses (`ConnectedEvent`, `DisconnectedEvent`, `PowerChangedEvent`, `VolumeChangedEvent`) and the `EventBus` feeding the WebSocket |

### `config/`

`Settings` via pydantic-settings (`PARTYBOXD_*` environment variables).

---

## `companion` — module reference

### `services/`

| File | Purpose |
|---|---|
| `audio.py` | `AudioService` — Bluetooth A2DP connection supervision and audio-readiness events |
| `bluez_dbus.py` | `BluezClient` — BlueZ D-Bus operations (pairing agent, A2DP connect, adapter control) |
| `pairing.py` | `PairingService` — scoped Bluetooth Classic pairing flow (ADR-027) |
| `spotify.py` | `SpotifyService` — librespot subprocess lifecycle |
| `provisioning.py` | `ProvisioningService` — WiFi captive-portal provisioning via NetworkManager |
| `router.py` | REST endpoints for audio, Spotify, volume, and the debug bundle |

AirPlay (`shairport-sync`) is planned post-v1.0 and will follow the same subprocess-manager pattern as `SpotifyService`.

### Top-level modules

| File | Purpose |
|---|---|
| `__main__.py` | Appliance entry point — composes the daemon app, Portal, services, and supervisor |
| `supervisor.py` | `Supervisor` — task supervision with restart policies and health tracking (ADR-024) |
| `volume.py` | `VolumeState` — logical volume authority (ADR-022) |
| `config.py` | `CompanionSettings` (`COMPANION_*` environment variables) |
| `config_store.py` | `ConfigStore` — persistent Portal configuration (`config.json`) |

### `webui/`

Serves the Companion Portal (single-page HTML app) at `/` and the config API at `/api/v1/config`.

### `wifi/`

WiFi provisioning REST endpoints (`/api/v1/wifi/*`) and the captive-portal middleware.

---

## Runtime model

`companion` extends `partyboxd`'s FastAPI app in-process:

```python
# companion/__main__.py (simplified)
from partyboxd.api import create_app as create_daemon_app

app = create_daemon_app(daemon_settings, manager)
app.include_router(make_services_router(spotify, config, ...))  # /api/v1/audio, /spotify, /volume, …
app.include_router(make_wifi_router(provisioning))              # /api/v1/wifi/*
app.include_router(make_portal_router(settings, store))         # Portal at /, /api/v1/config
```

Running `partyboxd` gives you the headless API. Running `partybox-companion` gives you the full appliance. Same routes, same port — companion adds to it. See [ADR-005](adr/005-appliance.md).

---

## Data flow

### State changes (outbound)

```
ControlTransport → protocol.codec → PartyBoxDevice
    │ observed by DeviceManager (connect/disconnect, command results)
    ▼
partyboxd EventBus (device/events.py)
    ├──► api/ WebSocket (/api/v1/events) → connected browsers
    └──► companion subscribers → VolumeState, service reactions
```

### Commands (inbound)

```
HTTP client (Companion Portal / HA / script)
    │ POST /api/v1/power/on
    ▼
partyboxd route handler → DeviceManager
    │ await device.power.turn_on()
    ▼
PartyBoxDevice → protocol.codec → ControlTransport → speaker
```

---

## Communication

Single HTTP server on one port — `partybox-companion` defaults to 8080 in dev and binds 80 on the appliance (see [ADR-017](adr/017-runtime-layout.md)); standalone `partyboxd` defaults to 8765. See [ADR-007](adr/007-tcp-only.md).

| Path | Content |
|---|---|
| `/` | Companion Portal |
| `/api/v1/` | REST API |
| `/api/v1/events` | WebSocket event stream |
| `/api/docs` | Interactive OpenAPI docs |

mDNS via system `avahi-daemon` → `http://partybox.local`.

API key auth via `X-Api-Key` header (`PARTYBOXD_API__API_KEY`). Optional — disabled by default on trusted networks. See [docs/api/v1.md](api/v1.md).

---

## Testing strategy

| Package | Module | Approach |
|---|---|---|
| `partybox` | `protocol/` | Pure unit tests; real BT captures as byte fixtures |
| `partybox` | `bluetooth/` | `MockTransport` with simulated drops and errors |
| `partybox` | `device/` | `MockTransport`; assert state and yielded events |
| `partybox` | `capabilities/` | Unit tests per capability; mock device responses |
| `partyboxd` | `api/` | FastAPI async test client (httpx); mock `DeviceManager` |
| `companion` | `services/` | Mock `asyncio.create_subprocess_exec` and BlueZ D-Bus |

Hardware tests (`@pytest.mark.hardware`) require a real PartyBox. Never run in CI.
