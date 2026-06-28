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

The image is compressed with `xz` at build time. Typical size: ~2–3 GB uncompressed, ~600–900 MB compressed.

### Release workflow: tag → draft release

The release pipeline is triggered by pushing a version tag (`v*.*.*`) to the main branch. It:

1. Runs the full CI suite (lint, type check, tests on Python 3.11 and 3.12)
2. Builds the appliance image
3. Publishes a **draft** GitHub Release with the compressed image attached

Draft releases are reviewed and published manually. This provides a window for the release manager to verify the image before it becomes publicly visible.

---

## Installation architecture

### One implementation, multiple invocations

`image/install.sh` is the single authoritative implementation of how Companion is installed. It is not merely a CI script — it is the definition of what a correctly installed Companion appliance looks like.

Every installation context must go through this script:

| Context | How install.sh is invoked |
|---|---|
| CI image build | arm-runner-action copies the repository into the QEMU image and calls `bash image/install.sh` |
| Manual install on a running Pi | `sudo PARTYBOX_SRC_DIR=$(pwd) bash image/install.sh` |
| Automated hardware tests (future) | Script invoked over SSH or in a test harness |
| Alternative installer (future) | Wrapper that prepares inputs and calls install.sh |

This architecture prevents installation paths from diverging. If a bug is found in how the BlueZ configuration is applied, it is fixed once, in install.sh, and all invocation contexts benefit.

**The corollary:** there should never be a second install script. When a new installation context is needed, the correct approach is to add an invocation mechanism for the existing script, not to write a parallel implementation.

### Future modularisation

The script is intentionally a single file today. It is readable and the coupling between sections is low. When it grows beyond ~400 lines, or when individual sections become reusable across contexts, it should be split into sourced modules:

```
image/
  install.sh             ← entry point (sources the modules below)
  modules/
    packages.sh          ← system package installation
    python.sh            ← uv + venv + Companion packages
    bluetooth.sh         ← BlueZ configuration
    networking.sh        ← WiFi power management, hostname
    services.sh          ← systemd units, Avahi
```

Each module is still a plain bash script. The entry point sets shared variables, sources each module in order, and handles cleanup. The invocation interface (`PARTYBOX_SRC_DIR`, `COMPANION_VERSION`) remains unchanged.

Do not split until it is necessary. Three similar sections are not a reason to abstract.

### uv installation and reproducibility

The current approach — downloading a pinned uv binary from GitHub releases — is deterministic on version but does not verify the binary's integrity. The full reproducibility path is:

```
download binary
    ↓
verify SHA256 against the published checksum file
    ↓
install
```

The GitHub Releases page for uv publishes `.sha256` files alongside each binary. When uv is next upgraded in install.sh, the checksum should be added at that point. This is not implemented today because it requires updating two values (version + checksum) in lockstep — the infrastructure is designed for it but the policy is deferred.

---

## Companion user

The `companion` service account is created as a **system user with no home directory** (`useradd --system --no-create-home`). This is the standard pattern for Linux service accounts that do not require interactive login, per-user configuration, or shell access.

**Why no home directory:**

- The service unit includes `ProtectHome=true`, which makes `/home` inaccessible to the process. A home directory under `/home/companion` would be created by the image build but unreachable at runtime — an inconsistency.
- The single mutable directory the service needs is `/var/lib/companion/`, which systemd creates and manages via `StateDirectory=companion` before `ExecStart`. This is the correct mechanism for persistent service state.
- The service unit exposes a runtime directory via `RuntimeDirectory=companion`, which creates `/run/companion/` (writable, tmpfs-backed, owned by companion) and sets `XDG_RUNTIME_DIR=/run/companion`. This is the correct location for PipeWire sockets, WirePlumber state, and any other user-session runtime artefacts.

**If future components require a home directory** (e.g. an interactive debug shell, or a tool that hard-codes `~/.config` paths and ignores `XDG_CONFIG_HOME`), the correct fix is to add `--home-dir /var/lib/companion --create-home` to the `useradd` call and remove `ProtectHome=true` from the service unit. This is a one-line change to install.sh. The decision is deferred until a concrete need arises.

---

## Hostname

The appliance default hostname is `partybox`. This is set at image build time in `/etc/hostname` and `/etc/hosts` and determines the mDNS address (`partybox.local`).

This is the **appliance default**, not a permanent value. The hostname is a standard Linux configuration file and can be changed at any time with `hostnamectl set-hostname <name>`. A future Portal feature allowing appliance rename should update `/etc/hostname` and restart `avahi-daemon`. This is straightforward — the current install does nothing that makes it difficult.

---

## Version management

The installed version is derived from the git tag and propagates automatically through every layer of the appliance. No version literal is ever edited by hand.

### Version flow

```
git tag v1.0.0
    │
    ▼ hatch-vcs reads the tag at pip-install time (inside install.sh)
    │
    ├─ partybox           1.0.0   ← importlib.metadata at runtime
    ├─ partyboxd          1.0.0   ← importlib.metadata at runtime
    └─ partybox-companion 1.0.0   ← importlib.metadata at runtime
              │
              ├── GET /api/v1/health   {"version": "1.0.0", ...}
              ├── Debug bundle         companion_version: 1.0.0
              └── Portal header        shows "1.0.0"

COMPANION_VERSION env var (set by release.yml from the tag)
    │
    ├── /etc/partybox-companion/version   ← plain text, written by install.sh
    └── /etc/motd                         ← sed-substituted at install time
```

### Mechanism

1. **`hatch-vcs`** is in `build-system.requires` in all three `pyproject.toml` files. When `uv pip install` runs inside the image, `hatch-vcs` reads the git tag from the copy of the repository at `/opt/partybox-src` and records it as the installed package version (`v1.0.0` → `1.0.0`). No tag → version `0.0.0.dev0` (configured via `fallback-version`).

2. At runtime, each package calls `importlib.metadata.version()` in its `__init__.py`. This reads the value recorded at install time — no knowledge of git is needed at runtime.

3. `partyboxd.__version__` is the canonical version string at the API layer. `GET /api/v1/health`, the debug bundle, and the Portal all read it from there.

4. `COMPANION_VERSION` is passed by the release workflow (from `${GITHUB_REF_NAME}`, the git tag). `install.sh` writes it to `/etc/partybox-companion/version` and substitutes it into `/etc/motd`. Both paths agree with the runtime version because they originate from the same tag.

### Single source of truth

The git tag is the only place a version number is set. There are no `__version__ = "x.y.z"` literals to maintain. Tagging a commit is the entire release act — everything downstream derives from it automatically.

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
- **Port 80 is not used.** The Portal is served on port 8080 until M14 resolves the port 80 binding strategy.
- **The developer workflow is unchanged.** `uv sync`, `uv run`, and the test suite work exactly as before. Image generation is purely a release engineering concern.
- **uv SHA256 verification is not implemented.** The binary is pinned by version; integrity checking is deferred to a future update of the uv version.

---

## Open items

- Configure PipeWire for the `companion` user via `loginctl enable-linger` or the ALSA backend alternative (audio routing for librespot)
- Image size optimisation (remove unnecessary Pi OS packages)
- First-boot hostname uniqueness (if multiple appliances are on the same network)
- Build time optimisation (apt cache layer)
- uv SHA256 verification (add alongside next uv version bump)
