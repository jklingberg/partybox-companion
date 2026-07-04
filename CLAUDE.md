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
companion  (appliance: Portal, service orchestration)
    ↑
clients    (browsers, Home Assistant, scripts)
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

The SDK exposes only hardware-unique capabilities that Spotify Connect, AirPlay, and AVRCP cannot provide. Play/pause and skip are **not** in the SDK — librespot and shairport-sync handle those natively. Hardware volume is the one exception: `VolumeCapability` exists per the volume authority model ([ADR-022](docs/adr/022-volume-authority.md)), but its BLE opcode is not yet confirmed and both methods raise `NotImplementedError`.

## Capability model

Capabilities are typed properties on `PartyBoxDevice` — plain classes, no shared base. Optional capabilities are `None` when unsupported; callers check for `None`:

```python
await speaker.power.turn_on()        # always present
if speaker.battery is not None:      # optional — portable models only
    level = await speaker.battery.level()
```

Adding a capability: create `device/capabilities/<name>.py` (follow `power.py` as the template), add a `@property` to `device/partybox.py` (typed `<Name>Capability | None` if optional), and export it from `partybox/__init__.py` if public.

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

## Raspberry Pi (hardware)

### SSH access

The appliance Pi is reachable at `pi@partybox.local` (mDNS, preferred) or `pi@partybox` (router DNS).

The default credentials are `pi` / `raspberry` (set by `install.sh`). Use `sshpass` for non-interactive access — it is installed in the devcontainer:

```bash
# One-off command
sshpass -p raspberry ssh -o StrictHostKeyChecking=no pi@partybox.local "<command>"

# rsync
sshpass -p raspberry rsync -e "ssh -o StrictHostKeyChecking=no" -av --delete <src> pi@partybox.local:<dst>
```

`StrictHostKeyChecking=no` avoids an interactive host-key prompt on first contact.

### `pi` vs `companion`: two separate users

SSH always connects as `pi`, but the appliance service runs as `companion` — a **different, more restricted account**. This is deliberate (see [ADR-019](docs/adr/019-distribution-approach.md)), not an oversight, so don't try to "fix" it by running things as `pi` or `root` — expect the split and work with `sudo` instead.

- `pi` — interactive login user (`useradd -m -s /bin/bash -G sudo pi`), passwordless sudo, has a home directory, owns the WirePlumber/PipeWire audio session at `/run/user/1000/`.
- `companion` — system account (`useradd --system --no-create-home --shell /usr/sbin/nologin`), **no shell, cannot log in**. Runs `companion.service` under systemd hardening (`NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=read-only`) plus `CAP_NET_BIND_SERVICE` to bind port 80. It has exactly one passwordless sudo grant of its own, `systemctl --user -M pi@ restart wireplumber` (used by `AudioService` to recover WirePlumber) — nothing else.

Ownership map — files under these paths are **not** readable/writable by `pi` without `sudo`:

| Path | Owner | Contents |
|---|---|---|
| `/var/lib/companion/` | `companion` | Portal state (`config.json`) |
| `/run/companion/` | `companion`, mode 0700 | Runtime dir (`XDG_RUNTIME_DIR` for the companion process) |
| `/etc/companion/companion.env` | `root` | Operator env overrides |
| `/run/user/1000/` | `pi` | PipeWire-pulse socket; chmod'd to 755 at service start so `companion` can reach it — see `companion.service` |

Practical commands when troubleshooting over SSH as `pi`:

```bash
# Read/list a companion-owned path
$SSH pi@partybox.local "sudo cat /var/lib/companion/config.json"
$SSH pi@partybox.local "sudo ls -la /run/companion"

# Run a one-off command as companion (works despite the nologin shell —
# sudo execs the command directly, it doesn't need an interactive login)
$SSH pi@partybox.local "sudo -u companion <command>"
```

Never attempt `ssh companion@partybox.local` — there is no shell to log into.

### Deploying source changes to the Pi

The appliance venv lives at `/opt/partybox-companion/` and is a `--no-editable` install (source copied into site-packages). To deploy a change without rebuilding the full image, rsync the relevant package directly into site-packages and restart the service.

Site-packages is **root-owned** on release images, so the remote rsync must run under sudo (`--rsync-path="sudo rsync"`); a plain rsync fails with `Permission denied (13)`.

```bash
SSH="sshpass -p raspberry ssh -o StrictHostKeyChecking=no"
RSYNC="sshpass -p raspberry rsync -e 'ssh -o StrictHostKeyChecking=no' --rsync-path='sudo rsync'"

# Deploy companion package changes
$RSYNC -a --delete --exclude='__pycache__' packages/companion/src/companion/ \
    pi@partybox.local:/opt/partybox-companion/lib/python3.14/site-packages/companion/

# Deploy partyboxd package changes
$RSYNC -a --delete --exclude='__pycache__' packages/partyboxd/src/partyboxd/ \
    pi@partybox.local:/opt/partybox-companion/lib/python3.14/site-packages/partyboxd/

# Deploy partybox SDK changes
$RSYNC -a --delete --exclude='__pycache__' packages/partybox/src/partybox/ \
    pi@partybox.local:/opt/partybox-companion/lib/python3.14/site-packages/partybox/

# Restart the service after any change
$SSH pi@partybox.local "sudo systemctl restart companion"
```

This is sufficient for Python source changes. For dependency changes (`pyproject.toml`, `uv.lock`) or changes to `install.sh`-managed files (systemd unit, BlueZ config, Avahi record), a full image rebuild and reflash is required.

### Service and log commands

```bash
SSH="sshpass -p raspberry ssh -o StrictHostKeyChecking=no"

# Service status
$SSH pi@partybox.local "systemctl status companion"

# Restart
$SSH pi@partybox.local "sudo systemctl restart companion"

# Health check
$SSH pi@partybox.local "curl -s http://localhost/api/v1/health"

# Follow logs
$SSH pi@partybox.local "journalctl -u companion -f"

# Last 100 lines
$SSH pi@partybox.local "journalctl -u companion -n 100 --no-pager"

# Bluetooth adapter reset (if GATT connections fail but scanning works)
$SSH pi@partybox.local "sudo systemctl restart bluetooth"
```

### Restarting the speaker or the Pi

**Speaker restart** — there is no dedicated restart endpoint; power-cycle it with the existing power endpoints (`packages/partyboxd/src/partyboxd/api/routes.py`):

```bash
curl -X POST -H "X-Api-Key: your-key" http://partybox.local/api/v1/power/off
sleep 2
curl -X POST -H "X-Api-Key: your-key" http://partybox.local/api/v1/power/on
```

Omit the `X-Api-Key` header if the appliance has no `api_key` configured (the default — auth is opt-in).

**Pi restart** is *not* exposed via the REST API — only the `companion` service can be restarted remotely (`sudo systemctl restart companion`, above). To reboot the underlying OS, use SSH directly:

```bash
$SSH pi@partybox.local "sudo reboot"
```

## Commit messages

Conventional Commits with these scopes: `bluetooth`, `protocol`, `device`, `capabilities`, `api`, `services`, `config`, `webui`, `docs`, `ci`
