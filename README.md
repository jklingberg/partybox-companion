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

## How it works

```
JBL PartyBox 520
      │  Bluetooth Classic (RFCOMM)
      ▼
┌──────────────────────────────────────────────┐
│  partybox-companion                          │
│                                              │
│  partybox  ─────────────────────────────┐   │
│  (BT transport · protocol · device)     │   │
│                                         │   │
│  partyboxd ─────────────────────────────┘   │
│  (HTTP server · REST API · WebSocket)       │
│                                             │
│  companion                                  │
│  (Portal · CLI · librespot · shairport)     │
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

```bash
# Scan for your PartyBox
partybox scan

# Check status (power, battery, firmware)
partybox status

# Power management
partybox power on
partybox power off

# Generate an API key
partybox generate-key

# Start the full appliance (Companion Portal + REST API at :80)
partybox-companion --config /etc/partybox-companion/partybox-companion.toml
```

The Companion Portal is then accessible at `http://partybox.local`.

## REST API

```bash
# SDK — use directly from Python without the daemon
python -c "
import asyncio
from partybox import PartyBox

async def main():
    speaker = await PartyBox.discover()
    await speaker.power.turn_on()
    print(await speaker.device_info.firmware_version())
    if speaker.battery is not None:
        print(await speaker.battery.level())

asyncio.run(main())
"

# Or use the REST API
curl -H "X-API-Key: your-key" http://partybox.local/api/v1/status
curl -X POST -H "X-API-Key: your-key" http://partybox.local/api/v1/power/on
```

OpenAPI docs available at `http://partybox.local/docs`.

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
| [`partybox`](packages/partybox/) | `partybox` | Bluetooth SDK. Zero dependencies. Usable without the daemon. |
| [`partyboxd`](packages/partyboxd/) | `partyboxd` | Headless daemon. HTTP API + WebSocket. No UI, no services. |
| [`companion`](packages/companion/) | `partybox-companion` | Full appliance. Companion Portal, `partybox` CLI, Spotify/AirPlay service managers. |

## Protocol compatibility

partybox-companion includes an independent implementation of the PartyBox Bluetooth protocol, developed through interoperability analysis. Protocol documentation and contributor notes live in [docs/reverse-engineering/](docs/reverse-engineering/). Contributions across models are welcome.

## Status

Active early development. Protocol is understood; software scaffold is in place.

Current milestone: **M2 — Bluetooth Transport**

See [CHANGELOG.md](CHANGELOG.md) for progress.

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgements

- [librespot](https://github.com/librespot-org/librespot) — open Spotify Connect implementation
- [shairport-sync](https://github.com/mikebrady/shairport-sync) — AirPlay audio player
- [Pi-hole](https://pi-hole.net/), [OctoPrint](https://octoprint.org/), [Homebridge](https://homebridge.io/) — inspiration for the appliance model
