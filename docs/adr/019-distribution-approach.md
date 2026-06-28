# ADR-019 — Distribution Approach

**Status:** Accepted
**Date:** 2026-06-28
**Milestone:** M13

---

## Context

The project's architectural direction (ADR-005) is a dedicated Raspberry Pi appliance. M13 is the milestone that turns that intent into a releasable distribution: a bootable image that an end user can flash to a microSD card and boot into a running Companion appliance, without any manual installation steps.

Several implementation approaches were evaluated.

---

## Decision

### Image base: Raspberry Pi OS Lite ARM64

We extend the official **Raspberry Pi OS Lite ARM64** base image rather than building from scratch. Pi OS is the community standard for Pi deployments — it has the correct BlueZ version, PipeWire packages, kernel drivers, and firmware for the target hardware out of the box. Building from scratch with pi-gen would duplicate maintenance work without benefit.

**Rejected:** pi-gen. It is the right tool for organisations building their own operating system distribution. For a project that is "Pi OS + our software", the full pi-gen framework adds complexity without value. Our image is a customisation of Pi OS, not a fork of it.

### Image build tool: arm-runner-action

The CI image build uses [`pguyot/arm-runner-action`](https://github.com/pguyot/arm-runner-action), a GitHub Actions action that:

1. Downloads the Pi OS Lite base image
2. Boots it inside QEMU with user-space ARM64 emulation (`qemu-user-static`)
3. Runs our customisation script (`image/install.sh`) inside the image
4. Returns the modified image as an artifact

This approach has two key properties:

- **Transparency.** `image/install.sh` is a plain bash script that documents exactly what is installed. Reviewers can read the script and know exactly what is in the image — no framework knowledge required.
- **Reproducibility.** The same script, the same base image, and the same git tag produce the same result on every CI run.

**Rejected:** pi-gen-action. A wrapper around pi-gen; inherits its complexity. The setup-stage-script model is less readable than a single install script for a project of this size.

**Rejected:** Custom QEMU chroot scripts. Equivalent in capability to arm-runner-action but requires ~200 lines of shell to manage loop devices, `qemu-user-static` registration, `binfmt_misc`, proc/sys/dev bind mounts, and image resizing. arm-runner-action encapsulates all of this and is well-maintained.

### librespot distribution: raspotify apt package

librespot is installed from the [raspotify](https://github.com/dtcooper/raspotify) apt repository, which provides a prebuilt ARM binary. The `raspotify.service` systemd unit is disabled immediately after package installation — Companion manages the librespot lifecycle directly via `SpotifyService` (see ADR-016). The `raspotify` package is used solely as a distribution mechanism for the librespot binary.

This satisfies the ADR-016 requirement: users install Companion, librespot is an implementation detail they never interact with.

**Rejected:** Compiling librespot from source during the image build. This requires a Rust toolchain inside the image and adds 20–30 minutes to the build. The raspotify binary is tested against the same target hardware.

### Python runtime: uv venv

The Python venv is created by `uv` at `/opt/partybox-companion/` (the production layout defined in ADR-017). The three workspace packages (`partybox`, `partyboxd`, `companion`) are installed from the source tree copied into the image. All transitive dependencies are resolved against the ARM64 target and downloaded from PyPI during the image build.

This means the image build requires network access — an accepted trade-off for a release pipeline running in CI.

### Release artifact: `.img.xz`

The primary distribution artifact is:

```
partybox-companion-vX.Y.Z.img.xz
```

This is the format expected by Raspberry Pi Imager. Users download the file, open Raspberry Pi Imager, select "Use Custom", write to their SD card, and boot. No additional software is required on their machine beyond Raspberry Pi Imager.

The image is compressed with `xz --best` at build time. Typical size: ~2–3 GB uncompressed, ~600–900 MB compressed.

### Release workflow: tag → draft release

The release pipeline is triggered by pushing a version tag (`v*.*.*`) to the main branch. It:

1. Runs the full CI suite (lint, type check, tests on Python 3.11 and 3.12)
2. Builds the appliance image
3. Publishes a **draft** GitHub Release with the compressed image attached

Draft releases are reviewed and published manually. This provides a window for the release manager to verify the image before it becomes publicly visible.

---

## Filesystem layout (after install)

The install script produces this layout, consistent with ADR-017:

```
/opt/partybox-companion/          Python venv (entry point + all packages)
/usr/local/bin/partybox-companion symlink into the venv
/usr/local/bin/librespot          raspotify-distributed binary
/usr/local/bin/uv                 Python toolchain
/lib/systemd/system/companion.service
/etc/companion/companion.env      operator overrides template
/etc/avahi/services/partyboxd.service
/etc/bluetooth/main.conf          AutoEnable=true
/etc/NetworkManager/conf.d/wifi-powersave-off.conf
/etc/partybox-companion/version   version string from the git tag
/etc/hostname                     partybox
```

---

## Consequences

- **CI build time.** A release build takes ~40–60 minutes: base image download (~5 min), apt package installation (~10 min), uv/Python install (~10 min), image compression (~5 min). This is acceptable for a release pipeline; developers are not blocked by it.
- **Base image version is not pinned in M13.1.** `raspios_lite_arm64:latest` is used. If a Pi OS release introduces a breaking change, the build could fail. M13.2 will pin to a specific dated release and verify the SHA256.
- **uv version is not pinned in M13.1.** The install.sh fetches the latest uv. M13.2 will pin the uv version for full reproducibility.
- **Port 80 is not used.** The Portal is served on port 8080 until M14 resolves the port 80 binding strategy.
- **The developer workflow is unchanged.** `uv sync`, `uv run`, and the test suite work exactly as before. Image generation is purely a release engineering concern.

---

## M13.2 scope

The following items are intentionally deferred to M13.2 (image polish):

- Pin the base Pi OS image to a specific dated release + SHA256
- Pin the uv version
- Configure PipeWire for the `companion` user (system-session audio routing for librespot)
- Image size optimisation (remove unnecessary Pi OS packages)
- Branding (custom boot splash, hostname confirmation message)
- First-boot hostname uniqueness (if multiple appliances are on the same network)
- Build time optimisation (apt cache layer, parallel compression)
