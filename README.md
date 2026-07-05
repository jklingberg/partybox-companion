# partybox-companion

Turn any JBL PartyBox into a true WiFi speaker — no cloud, no subscription, no app required.

Plug a Raspberry Pi into your PartyBox. Flash an SD card. Visit `http://partybox.local`. Done.

> This is an independent community-developed project. It is not affiliated with, endorsed by, or sponsored by JBL or HARMAN International.

---

## What it does

A Raspberry Pi sits next to your PartyBox and communicates with it over Bluetooth. The companion upgrades the speaker with features the factory firmware never provided:

- **Spotify Connect** — stream from any Spotify client via [librespot](https://github.com/librespot-org/librespot)
- **AirPlay** — stream from Apple devices via [shairport-sync](https://github.com/mikebrady/shairport-sync)
- **Companion Portal** — configure, monitor, and troubleshoot the appliance at `http://partybox.local`
- **REST API** — open HTTP API for scripts, automations, and third-party integrations
- **Bluetooth management** — auto-reconnects after power cycles, connects reliably
- **Power management** — turn the speaker on/off, monitor battery status
- **Home Assistant** — integrates as a standard HTTP client; no special support required

## Hardware

| Component | Minimum | Target |
|-----------|---------|--------|
| SBC | Raspberry Pi 3 B+ | Raspberry Pi Zero 2 W |
| OS | Raspberry Pi OS Lite / Debian 12 | Raspberry Pi OS Lite (64-bit) |
| Bluetooth | Built-in or USB dongle | Built-in |
| Speaker | JBL PartyBox 520 | Any supported PartyBox model |

Standard Linux Bluetooth APIs only (BlueZ). Not Raspberry Pi specific — runs on any ARM/x86 Linux SBC.

### Verified compatibility

The table above lists *requirements*. This table lists combinations we have actually run on real hardware. The Bluetooth controller on the host matters as much as the speaker: controller quirks are host-specific and are not something the SDK can paper over (see [ADR-028](docs/adr/028-audio-readiness-model.md)).

| Raspberry Pi | BT controller | Speaker | Status | Notes |
|---|---|---|---|---|
| Pi 3 B+ | Broadcom (on-board) | JBL PartyBox 520 | ✅ Verified | Only combination validated on hardware to date |

Legend: ✅ Verified (run end-to-end on hardware) · 🟡 Community-reported (works, not verified by us) · ⬜ Untested

Other Pi models (Zero 2 W, Pi 4) and other PartyBox models are expected to work but are **untested** — the design is capability-based and does not branch on model. If you run a combination not listed here, please report it (see [CONTRIBUTING.md](CONTRIBUTING.md)); captures from an untested combination are valuable.

## How it works

```
JBL PartyBox 520
      ▲  BLE GATT (control)  +  Bluetooth Classic A2DP (audio)
      │
┌──────────────────────────────────────────────┐
│  partybox-companion                          │
│                                              │
│  partybox  ─────────────────────────────┐   │
│  (BLE transport · protocol · device)    │   │
│                                         │   │
│  partyboxd ─────────────────────────────┘   │
│  (HTTP server · REST API · WebSocket)       │
│                                             │
│  companion                                  │
│  (Portal · service orchestration · librespot)│
└──────────────────────────────────────────────┘
                     │
              http://partybox.local
                     │
         ┌───────────┼───────────┐
         ▼           ▼           ▼
      Portal    REST API    WebSocket
         │           │
      Browser    HA / scripts / apps
```

See [docs/architecture.md](docs/architecture.md) for full design.

## Quick Start

> Full installation guide coming in v1.0. The steps below reflect the target experience.

1. Flash the appliance image to an SD card and boot the Raspberry Pi.
2. Join its WiFi setup network and enter your home WiFi credentials (captive portal).
3. Open the Companion Portal at `http://partybox.local`.
4. Pair the speaker over Bluetooth from the Portal, then start streaming with Spotify Connect.

To run the appliance directly from a source checkout:

```bash
# Start the full appliance (Companion Portal + REST API on port 80)
COMPANION_PORT=80 uv run partybox-companion
```

The Companion Portal is then accessible at `http://partybox.local`. Manage the
appliance from there or via the [REST API](docs/api/v1.md).

## REST API

```python
# SDK — use directly from Python without the daemon
import asyncio
from partybox import Scanner

async def main():
    speaker = await Scanner.find()
    if speaker is None:
        print("No PartyBox found")
        return
    async with speaker:
        await speaker.power.turn_on()
        print(await speaker.device_info.firmware_version())
        if speaker.battery is not None:
            print(await speaker.battery.level())

asyncio.run(main())
```

```bash
# Or use the REST API
curl http://partybox.local/api/v1/health
curl -H "X-Api-Key: your-key" http://partybox.local/api/v1/speaker
curl -X POST -H "X-Api-Key: your-key" http://partybox.local/api/v1/power/on
```

OpenAPI docs available at `http://partybox.local/api/docs`; full reference in [docs/api/v1.md](docs/api/v1.md).

## Development

This project uses [uv](https://docs.astral.sh/uv/) for dependency management.

```bash
git clone https://github.com/jklingberg/partybox-companion
cd partybox-companion
uv sync --all-extras
uv run pre-commit install

# Checks
uv run ruff check .
uv run mypy src/  # run from each package directory

# Tests
uv run pytest packages/partybox/
uv run pytest packages/partyboxd/
uv run pytest packages/companion/
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full guide.

## Packages

| Package | PyPI name | Description |
|---------|-----------|-------------|
| [`partybox`](packages/partybox/) | `partybox` | Bluetooth SDK. Depends only on `bleak`. Usable without the daemon. |
| [`partyboxd`](packages/partyboxd/) | `partyboxd` | Headless daemon. HTTP API + WebSocket. No UI, no services. |
| [`companion`](packages/companion/) | `partybox-companion` | Full appliance. Companion Portal, Spotify Connect + Bluetooth audio + WiFi provisioning orchestration. |

## Protocol compatibility

partybox-companion includes an independent implementation of the PartyBox Bluetooth protocol, developed through interoperability analysis. Protocol documentation and contributor notes live in [docs/reverse-engineering/](docs/reverse-engineering/). Contributions across models are welcome.

## Status

Approaching v1.0. The protocol is understood, the daemon and Companion Portal
are functional, and the appliance runs on real hardware (Spotify Connect over
Bluetooth A2DP with BLE control). Remaining work is release hardening.

See [CHANGELOG.md](CHANGELOG.md) for progress and [docs/roadmap.md](docs/roadmap.md) for what's deferred past v1.0.

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgements

- [librespot](https://github.com/librespot-org/librespot) — open Spotify Connect implementation
- [shairport-sync](https://github.com/mikebrady/shairport-sync) — AirPlay audio player
- [Pi-hole](https://pi-hole.net/), [OctoPrint](https://octoprint.org/), [Homebridge](https://homebridge.io/) — inspiration for the appliance model
