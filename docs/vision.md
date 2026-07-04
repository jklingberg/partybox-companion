# Vision

## What this project is

partybox-companion turns any JBL PartyBox into a smart WiFi speaker using an inexpensive Linux SBC such as a Raspberry Pi.

The ideal user journey:

1. Flash an SD card.
2. Configure WiFi.
3. Plug the Pi into the speaker.
4. Open `http://partybox.local`.
5. Complete setup in the Companion Portal. Enjoy Spotify Connect and AirPlay.

That's it. No cloud account. No JBL app. No subscription.

## What this project is not

**Not a Home Assistant integration.** Home Assistant can connect to the REST API like any other HTTP client. It is one consumer among many, not the primary target.

**Not a Python library.** The `partybox` SDK is a publishable library, but it exists to support the appliance — not the other way around.

**Not a Bluetooth utility.** The Bluetooth implementation is a means to an end. The end is a plug-and-play appliance that anyone can use.

## Who this is for

The primary user is someone who owns a JBL PartyBox and wants it to behave like a smart speaker without giving up privacy, paying a subscription, or depending on JBL's cloud infrastructure.

They are comfortable flashing an SD card and plugging in a Raspberry Pi. They do not need to write code.

The secondary user is a developer who wants programmatic access to their PartyBox — via the `partybox` SDK directly or the REST API.

## Why this exists

JBL PartyBox speakers are capable hardware with good audio quality. Their smart features are locked behind a mobile app and cloud service that may be discontinued, and do not integrate with standard smart home systems.

JBL PartyBox speakers communicate over a Bluetooth protocol that is not publicly documented. This project developed an independent implementation of that protocol through interoperability analysis, and built an open appliance on top of it — making it available to the community rather than requiring every user to rediscover it.

## Design principles

**Appliance-first.** Every architectural decision should serve the user who just wants to plug it in and have it work.

**Layers, not a monolith.** Each layer (SDK, daemon, appliance) is independently useful and can be adopted without the rest.

**Orchestrate, don't reimplement.** Spotify Connect is librespot. AirPlay is shairport-sync. The appliance manages them; it does not replace them.

**Use existing protocols.** When a mature open protocol already solves a problem well — volume via Spotify Connect, playback control via AVRCP — use it. The proprietary Bluetooth SDK covers only what those protocols cannot: power management, battery status, lighting, karaoke, and other hardware-unique features. The filter question is: *does this make the PartyBox a better WiFi speaker in a way that an existing open protocol cannot?*

**Capability-based, not model-specific.** The same code should work for every PartyBox model. New models are new capability configurations, not new code paths.

**Open interoperability.** The protocol documentation and independent implementation are first-class deliverables alongside the software. Every PartyBox owner benefits when the integration work is done in the open rather than rediscovered in private.
