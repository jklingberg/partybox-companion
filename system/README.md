# System integration

Host-level configuration for running partybox-companion on a Raspberry Pi
appliance. These are **not** applied automatically yet — codifying them into the
v1.0 image build is tracked under the release criteria in the [roadmap](../docs/roadmap.md).

## Files

| File | Purpose |
|---|---|
| `systemd/partyboxd.service` | systemd unit for the daemon |
| `avahi/partyboxd.service` | mDNS record (`partybox.local`) |

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

Then restart BlueZ (`systemctl restart bluetooth`).

> **Status:** Currently applied **manually** on the test Pi (BlueZ 5.82). It must
> be baked into the v1.0 image, OR the daemon should ensure the adapter is
> powered at startup (a candidate for M6, since the daemon owns the Bluetooth
> connection). Until then, a reflash of the SD card reverts this and it must be
> re-applied by hand.
