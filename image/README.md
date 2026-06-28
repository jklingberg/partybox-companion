# Image build

This directory contains the release engineering infrastructure for producing a
bootable Raspberry Pi OS appliance image.

**The primary distribution artifact is the appliance image.** It is what end users
flash to a microSD card. Everything here exists to produce that image reliably and
reproducibly.

## Installation architecture

`install.sh` is the **single authoritative implementation** of how Companion is
installed. All installation contexts use it — CI image build, manual Pi install, and
any future contexts (test harnesses, alternative installers). See [ADR-019](../docs/adr/019-distribution-approach.md) for
the full rationale.

## What lives here

| Path | Purpose |
|---|---|
| `install.sh` | The installation implementation — run inside the Pi OS image during CI, or on a running Pi |
| `smoke-test.sh` | Release verification — starts Companion and checks `GET /api/v1/health`; called by the release workflow after install.sh |
| `config/base-image.env` | Pinned Pi OS base image (version, name, URL) — update here when upgrading the base image |
| `config/wifi-powersave.conf` | NetworkManager drop-in that disables WiFi power saving |
| `config/motd` | SSH login message template (version is substituted at install time) |

The service unit, env template, and Avahi record live in [`system/`](../system/) because
they are runtime configuration files — `install.sh` copies them into the image from there.

## How images are built

Images are built automatically by the [release workflow](../.github/workflows/release.yml)
when a version tag is pushed:

```
git tag v1.0.0
git push origin v1.0.0
```

The workflow:
1. Runs the full CI suite (lint, type check, tests)
2. Downloads the Raspberry Pi OS Lite ARM64 base image (pinned in `config/base-image.env`)
3. Boots it inside QEMU via [arm-runner-action](https://github.com/pguyot/arm-runner-action)
4. Runs `image/install.sh` inside the image to install Companion and all dependencies
5. Runs `image/smoke-test.sh` inside the image — starts Companion, calls `GET /api/v1/health`, and verifies the response
6. Compresses the result with xz
7. Publishes a **draft** GitHub Release containing `partybox-companion-vX.Y.Z.img.xz`

Step 5 is a gate: if the health endpoint does not respond within 30 seconds, or returns a non-`ok` status, the workflow fails and no release is published.

The draft release is reviewed and published manually. This gives the release manager
a window to add release notes before the image is publicly visible.

## What install.sh does

`install.sh` transforms a stock Pi OS Lite ARM64 image into a Companion appliance.
It is designed to be readable — the script documents exactly what is in the image.

Steps (in order):

1. **System packages** — `pipewire`, `bluez`, `avahi-daemon`, and runtime dependencies
2. **librespot** — installed from the [raspotify](https://github.com/dtcooper/raspotify) apt repository; `raspotify.service` is disabled immediately (Companion manages the lifecycle — see ADR-016)
3. **companion user** — system user with no login shell, in the `bluetooth` group
4. **uv** — installed to `/usr/local/bin`; used to create the Python venv
5. **Companion venv** — `uv venv /opt/partybox-companion --python python3`; installs partybox, partyboxd, companion from source
6. **systemd service** — `companion.service` copied and enabled (auto-starts on boot)
7. **Avahi** — mDNS service record so `partybox.local` resolves
8. **BlueZ** — `AutoEnable=true` so the Bluetooth adapter powers on automatically
9. **WiFi power management** — disabled to keep mDNS reliable during A2DP streaming
10. **Hostname** — set to `partybox`
11. **Version record** — written to `/etc/partybox-companion/version`

## Manual installation on a running Pi

> **Audience:** contributors debugging install.sh, developers without SD card access,
> or advanced users who prefer a direct install over flashing. The appliance image is
> the primary and supported deployment path. If in doubt, flash the image.

Run as root from the repository root on a Pi with Pi OS Lite already installed:

```bash
sudo PARTYBOX_SRC_DIR=$(pwd) bash image/install.sh
```

After the script completes, start the service:

```bash
sudo systemctl start companion
```

The manual install is equivalent to the image build — it runs the same `install.sh` with
the same effect. Updates to the installation logic benefit both contexts automatically.

## Filesystem layout after installation

```
/opt/partybox-companion/          <- Python venv (ADR-017)
    bin/partybox-companion
    lib/python3.x/site-packages/

/usr/local/bin/
    partybox-companion             <- symlink to /opt/…/bin/partybox-companion
    librespot                      <- from raspotify (managed by SpotifyService)
    uv                             <- Python toolchain

/lib/systemd/system/
    companion.service

/etc/
    bluetooth/main.conf            <- AutoEnable=true
    companion/companion.env        <- operator overrides (see system/README.md)
    avahi/services/partyboxd.service
    NetworkManager/conf.d/wifi-powersave-off.conf
    partybox-companion/version     <- version string from the git tag

/var/lib/companion/               <- created by systemd StateDirectory at first boot
    config.json                    <- Portal config (written by the running service)
```

## Building locally (advanced)

You can run the same QEMU-based build locally with Docker. This requires approximately
3–4 GB of free disk space and 20–40 minutes to complete.

Prerequisites: Docker with `binfmt_misc` support for ARM64.

```bash
# Build the image locally using the same base as CI
docker run --rm --privileged multiarch/qemu-user-static --reset -p yes
# ... see arm-runner-action documentation for local invocation
```

For day-to-day development, use `sudo bash image/install.sh` on a Pi rather than
rebuilding the full image. The full image build is reserved for releases.

## Release cadence

- `v*.*.*` tags produce release images
- Images are published as **draft** releases — review and publish manually
- Each release image is pinned to the git tag that produced it

See [docs/roadmap.md](../docs/roadmap.md) for the milestone plan.
