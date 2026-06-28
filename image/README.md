# Image build

This directory contains the release engineering infrastructure for producing a
bootable Raspberry Pi OS appliance image.

## What lives here

| Path | Purpose |
|---|---|
| `install.sh` | Appliance setup script — runs inside the Pi OS image during CI, or manually on a Pi |
| `config/wifi-powersave.conf` | NetworkManager drop-in that disables WiFi power saving |

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
2. Downloads the latest Raspberry Pi OS Lite ARM64 base image
3. Boots it inside QEMU via [arm-runner-action](https://github.com/pguyot/arm-runner-action)
4. Runs `image/install.sh` inside the image to install Companion and all dependencies
5. Compresses the result with xz
6. Publishes a **draft** GitHub Release containing `partybox-companion-vX.Y.Z.img.xz`

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

`install.sh` can also be run directly on a Raspberry Pi that already has Pi OS Lite installed.
Run as root from the repository root:

```bash
sudo PARTYBOX_SRC_DIR=$(pwd) bash image/install.sh
```

This is useful for testing the install script without a full image build, or for
updating an existing Pi installation during development.

After running the script, restart the service:

```bash
sudo systemctl restart companion
```

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
