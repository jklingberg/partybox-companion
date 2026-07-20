# partybox-companion

**Turn your JBL PartyBox into a real WiFi speaker.**

Add Spotify Connect. Control it from a web page on your own network. Nothing leaves your LAN.

No cloud · No subscriptions · No proprietary app

> This is an independent community-developed project. It is not affiliated with, endorsed by, or sponsored by JBL or HARMAN International.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

---

![Companion Portal walkthrough: dashboard, renaming the Spotify Connect device in Settings, and the System health screen](docs/images/portal-demo.gif)

## Why

JBL PartyBox speakers sound great but ship as Bluetooth-only appliances: no Spotify Connect, and a companion app that phones home to JBL's cloud for features that have nothing to do with cloud services. partybox-companion replaces that app entirely. Plug a Raspberry Pi into the speaker, flash an SD card, and the PartyBox becomes a proper network speaker — one that shows up in Spotify's device list like any other WiFi speaker, controllable from a phone, and manageable from a plain web page on your own network. Nothing leaves your LAN.

## Features

- **Spotify Connect** — stream from any Spotify client, no separate app needed ([librespot](https://github.com/librespot-org/librespot))
- **Companion Portal** — a clean web app for setup, status, and troubleshooting — no install, no account
- **REST API** — script it, automate it, or plug it into Home Assistant as a plain HTTP integration
- **Power & battery control** — turn the speaker on/off and check battery level remotely
- **Stays connected** — reconnects automatically after power cycles and Bluetooth hiccups
- **AirPlay** — *on the roadmap* for post-v1.0 ([docs/roadmap.md](docs/roadmap.md))

## Quick Start

Up and running in about five minutes:

1. **Flash** the appliance image to an SD card and boot the Pi.
2. **Connect** — join the `PartyBox Companion Setup` WiFi network and pick your home WiFi.
3. **Open** the Companion Portal at `http://partybox.local`.
4. **Pair** — hold the Bluetooth button on the PartyBox until it flashes, then tap **Start Pairing**.

That's it. The appliance now shows up as **PartyBox Companion** in your Spotify Connect device list.

<details>
<summary>Common first-install snags</summary>

- **Spotify Connect appears only after pairing succeeds.** The appliance hides itself from Spotify clients until it can actually play audio, so "no Spotify device" almost always means step 4 hasn't completed.
- **`partybox.local` doesn't always resolve.** It depends on your router and device supporting mDNS, not on anything the appliance controls. Try `http://partybox`, or fall back to the Pi's IP address.
- **No sound, but everything shows connected?** Another Bluetooth device — often a phone that auto-reconnected — may be holding the speaker's audio channel. Disconnect it and play again.
- **Speaker refuses to pair, or keeps reconnecting to an old phone?** This resets the *speaker's* own Bluetooth state, not Companion's — try the lighter option first: hold the **Bluetooth** button on the PartyBox for 10+ seconds to drop its current pairing and make it ready for a new one. If that's not enough, a full factory reset of the speaker (pairing, light patterns, EQ — everything) is holding **Play** + **Light** together for 10+ seconds until you hear a confirmation tone. Neither of these touches the Companion Portal's own "Factory reset" in Settings, which only resets the appliance side.

</details>

To run the appliance directly from a source checkout instead of flashing an image:

```bash
# Companion Portal + REST API, default port 8080 (a plain `uv run` has no
# permission to bind port 80 — the flashed appliance image grants that via
# systemd, so it defaults to 80 there without any extra configuration)
uv run partybox-companion
```

## Supported hardware

| Component | Minimum | Target |
|-----------|---------|--------|
| SBC | Raspberry Pi 3 B+ | Raspberry Pi Zero 2 W |
| OS | Raspberry Pi OS Lite / Debian 12 | Raspberry Pi OS Lite (64-bit) |
| Bluetooth | Built-in or USB dongle | Built-in |
| Speaker | JBL PartyBox 520 | Any supported PartyBox model |

Just standard Linux Bluetooth (BlueZ) underneath, so it isn't Raspberry Pi specific — any ARM/x86 Linux SBC works.

| Raspberry Pi | Speaker | Status |
|---|---|---|
| Pi 3 B+ | JBL PartyBox 520 | ✅ Validated end-to-end |
| Pi 5 | JBL PartyBox 520 | ✅ Validated end-to-end |

Other Pi models and other PartyBox models are expected to work — the design is capability-based and doesn't branch on model — but are untested. See [docs/model-support.md](docs/model-support.md) for how capability detection works, and please [report your hardware](CONTRIBUTING.md) if you try a combination not listed here.

### Powering the Pi from the speaker

On the PartyBox 520, the rear USB-C port is a real USB-C PD power source, not just a charge-my-phone afterthought — it can power the Pi directly, so a separate USB power supply for the Pi isn't necessary. Per JBL's own spec sheet, it outputs:

| Profile | Voltage / current |
|---|---|
| PDO | 5V/3A, 9V/3A, 15V/2A, 20V/1.5A |
| PPS | 5–11V/2.7A, 5–16V/1.85A |

Whether that covers your Pi:

| Raspberry Pi | Official power requirement | Powered by the 520's USB-C? |
|---|---|---|
| Zero 2 W | 5V/1.2–2.5A, Micro-USB | ✅ Comfortably — needs a USB-C-to-Micro-USB cable |
| 3 B+ | 5V/2.5A, Micro-USB | ✅ Comfortably — needs a USB-C-to-Micro-USB cable |
| 4 | 5.1V/3A, USB-C | ✅ Works — its 5V/3A profile is what most 5V/3A PD supplies provide anyway |
| 5 | 5V/5A recommended, USB-C | ⚠️ Boots and runs, but under the recommended supply — expect the low-voltage warning icon and USB peripherals capped to 600mA |

Only the 520 is verified here (it's the only model this project tests against). Other current models we checked — PartyBox 310, PartyBox Ultimate — only expose a plain 5V/2.1A USB output with no PD ladder: enough for a Zero 2 W, marginal for a 3 B+, not enough for a 4 or 5. If you've measured a different model, a PR to this table is welcome.

## Companion Portal

The Portal is a single-page web app served by the appliance itself — no separate install, no account, no companion mobile app. Open `http://partybox.local` from any device on your network to:

- Check speaker and connection status at a glance
- Configure the Spotify Connect device name
- Re-run WiFi provisioning or speaker pairing
- Download a debug bundle for troubleshooting

## REST API & SDK

Everything the Portal does is backed by a documented HTTP API, usable directly from scripts, Home Assistant, or any HTTP client:

```bash
curl http://partybox.local/api/v1/health
curl -H "X-Api-Key: your-key" http://partybox.local/api/v1/speaker
curl -X POST -H "X-Api-Key: your-key" http://partybox.local/api/v1/power/on
```

Authentication is opt-in and off by default; set it with `PARTYBOXD_API__API_KEY=your-key` when starting the daemon, then use the same value as `your-key` above. Interactive docs are served at `http://partybox.local/api/docs`; full reference in [docs/api/v1.md](docs/api/v1.md).

The Bluetooth layer is also a standalone Python SDK, usable without the daemon or appliance:

```python
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

## How it's built

partybox-companion is three Python packages layered strictly one way — a Bluetooth SDK, a headless HTTP daemon built on it, and an appliance layer that adds the Portal and streaming services on top:

| Package                            | PyPI name            | Description                                                           |
| ---------------------------------- | -------------------- | --------------------------------------------------------------------- |
| [`partybox`](packages/partybox/)   | `partybox`           | Bluetooth SDK. Depends only on `bleak`. Usable standalone.            |
| [`partyboxd`](packages/partyboxd/) | `partyboxd`          | Headless daemon. REST API + WebSocket. No UI, no services.            |
| [`companion`](packages/companion/) | `partybox-companion` | Full appliance. Companion Portal, Spotify Connect, WiFi provisioning. |

Speaker control runs over BLE GATT rather than classic Bluetooth SPP — see [docs/architecture.md](docs/architecture.md) for the full design and [docs/adr/](docs/adr/) for the reasoning behind it. The protocol itself is an independent implementation developed through interoperability analysis; details live in [docs/reverse-engineering/](docs/reverse-engineering/).

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
uv run pytest packages/partybox/   -m "not hardware"
uv run pytest packages/partyboxd/  -m "not hardware"
uv run pytest packages/companion/  -m "not hardware"
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full guide, including how to contribute captures for untested speaker models.

## Project status

partybox-companion runs end-to-end on real hardware — Spotify Connect streaming over Bluetooth A2DP with BLE control, WiFi provisioning, and the Companion Portal, all validated together on a Raspberry Pi and JBL PartyBox 520. Core functionality is complete. What's left before v1.0 is release hardening: broader hardware validation and final polish.

See [CHANGELOG.md](CHANGELOG.md) for progress and [docs/roadmap.md](docs/roadmap.md) for what's deferred past v1.0.

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgements

- [librespot](https://github.com/librespot-org/librespot) — open Spotify Connect implementation
- [shairport-sync](https://github.com/mikebrady/shairport-sync) — AirPlay audio player
- [Pi-hole](https://pi-hole.net/), [OctoPrint](https://octoprint.org/), [Homebridge](https://homebridge.io/) — inspiration for the appliance model
