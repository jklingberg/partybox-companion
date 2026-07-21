# Installation

The full install walkthrough, with troubleshooting. For the condensed version, see the [Quick Start](../README.md#quick-start) in the README.

## Requirements

- Raspberry Pi 3 B+ or newer — see [Supported hardware](../README.md#supported-hardware) for the full compatibility matrix
- MicroSD card — 8 GB minimum, 16 GB recommended
- A JBL PartyBox speaker — the PartyBox 520 is the only model this project currently tests against; other models are expected to work (the design is capability-based, not model-specific) but are untested, see [docs/model-support.md](model-support.md)
- 2.4 GHz or 5 GHz WiFi network

## 1. Flash the image

1. Download the appliance image (`.img.xz`) from the [latest release](https://github.com/jklingberg/partybox-companion/releases/latest)
2. Open [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
3. **Device** → select your Pi model
4. **OS** → Use custom → select the downloaded file
5. **Storage** → select your SD card
6. Write the image, insert the card, and power on the Pi — first boot takes 30–60 seconds

## 2. Connect it to your WiFi

1. On your phone or laptop, open the WiFi network list and join **`PartyBox Companion Setup`** — wait for the captive portal to appear
2. Select your home WiFi SSID from the list and enter the password

The setup network disappears while the Pi joins your WiFi — success and failure look the same from your device. If the setup network **reappears** after a minute, the join failed (usually a mistyped password): reconnect to it and the portal shows the reason so you can retry.

## 3. Open the Companion Portal

Open `http://partybox.local` from a device on your home network.

Neither this nor the fallback below is guaranteed to work on every network — both depend on your router and device, not on anything the appliance controls, and either can stop resolving later even if it worked at first (a router reboot, guest WiFi, or a device without Bonjour/mDNS support are common causes, not an appliance fault).

- If it doesn't resolve, try `http://partybox` (works when your router auto-registers DHCP hostnames)
- If neither works, use the Pi's IP address from your router's device list — that always works and is worth noting down

## 4. Pair your speaker

A fresh install has never paired with your speaker. Pairing is stored on the SD card (`/var/lib/companion/config.json`), not in the image, so redo this after every reflash, or when moving to a new Pi or a new card, even if the same speaker worked before.

1. Open the Portal — it shows a **Pair your speaker** screen until this is done
2. Press the Bluetooth button on the PartyBox until its LEDs flash (pairing mode)
3. Tap **Start Pairing** right away — JBL's pairing window is short, so put the speaker in pairing mode just before tapping the button. The scan can take up to 60 seconds

Spotify Connect appears only after pairing succeeds — the appliance deliberately hides itself from Spotify clients until it can actually play audio, so "no Spotify device" almost always means this step hasn't completed. (BLE control — power, battery — connects automatically without pairing; only the audio link needs it.)

## Troubleshooting

- **No sound, but everything shows connected?** Another Bluetooth device — often a phone that auto-reconnected — may be holding the speaker's audio channel. Disconnect it (or turn off Bluetooth on it) and play again.
- **Speaker refuses to pair, or keeps reconnecting to an old phone?** This resets the *speaker's* own Bluetooth state, not Companion's — try the lighter option first: press the Bluetooth button on the PartyBox for 10+ seconds to drop its current pairing and make it ready for a new one. If that's not enough, a full factory reset of the speaker (pairing, light patterns, EQ — everything) is holding Play + Light together for 10+ seconds until you hear a confirmation tone. Neither of these touches the Companion Portal's own "Factory reset" in Settings, which only resets the appliance side.

---

See the [contributor guide](../CONTRIBUTING.md) for setting up a development environment, and [image/README.md](../image/README.md) for how the appliance image itself is built.
