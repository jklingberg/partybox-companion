# System integration

Host-level configuration for running partybox-companion on a Raspberry Pi appliance.

These files are installed by the image build (M13). They are not applied automatically during development.

## Files

| File | Installed to | Purpose |
|---|---|---|
| `systemd/companion.service` | `/lib/systemd/system/` | systemd service unit |
| `systemd/companion.env` | `/etc/companion/companion.env` | Operator configuration template |
| `avahi/partyboxd.service` | `/etc/avahi/services/` | mDNS record (`partybox.local`) |

## Manual installation (Pi development workflow)

To install on the Pi without a full image build:

```bash
# Copy files
sudo cp system/systemd/companion.service /lib/systemd/system/
sudo mkdir -p /etc/companion
sudo cp system/systemd/companion.env /etc/companion/companion.env

# Edit the env file — at minimum set COMPANION_AUDIO__SINK_ADDRESS
sudo nano /etc/companion/companion.env

# Create the companion user
sudo useradd --system --no-create-home --shell /usr/sbin/nologin companion
sudo usermod -aG bluetooth companion

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable --now companion

# Check status
sudo systemctl status companion
journalctl -u companion -f
```

The service expects the appliance to be installed at `/opt/partybox-companion/`. During
development, update `ExecStart` in the unit to point at your `uv` venv if needed.

## Host requirements

### Bluetooth adapter auto-enable

The BLE control transport needs the Bluetooth controller **powered on at boot**.
On a stock Raspberry Pi OS / BlueZ install the adapter can come up `DOWN`, which
makes discovery and connection fail until it is manually brought up.

Enable BlueZ's auto-enable policy so the controller powers on automatically:

```ini
# /etc/bluetooth/main.conf
[Policy]
AutoEnable=true
```

Then restart BlueZ (`sudo systemctl restart bluetooth`).

> **Status:** Currently applied **manually** on the test Pi (BlueZ 5.82). This must
> be baked into the v1.0 image. Until then, a reflash of the SD card reverts this
> and it must be re-applied by hand.

### WiFi power management

On Pi 3 B+, disable WiFi power management to reduce mDNS unreliability during
active A2DP streaming (see v1.0 known limitations in the roadmap):

```ini
# /etc/NetworkManager/conf.d/wifi-powersave-off.conf
[connection]
wifi.powersave = 2
```
