# ADR-010: SDK Scope — Hardware-Unique Features Only

**Status:** Accepted

---

## Context

The JBL PartyBox Bluetooth protocol exposes many capabilities. Some are unique to the hardware (lighting effects, karaoke, battery status, firmware information). Others duplicate functionality that mature open protocols already provide well:

- **Volume / mute** — handled by librespot (Spotify Connect), shairport-sync (AirPlay), and Bluetooth AVRCP
- **Play / pause / skip** — handled by AVRCP
- **Stream quality / audio format** — handled by the streaming protocol itself

The question is whether the SDK should expose these media playback controls through the proprietary Bluetooth protocol.

## Decision

The `partybox` SDK focuses exclusively on hardware-unique capabilities that no open protocol provides.

**In scope:**
- Power management (on/off, power state)
- Battery status and charging state
- Device information (firmware version, model)
- Lighting control
- Microphone and karaoke features
- Sound modes and hardware-level EQ presets
- Input source selection
- PartyBoost / Auracast coordination
- Any other hardware-specific feature absent from open protocols

**Out of scope for the SDK:**
- Volume control — librespot and shairport-sync expose this through their own protocols; AVRCP handles it for Bluetooth audio
- Play / pause / next / previous — AVRCP
- Media metadata — AVRCP
- Any feature where a mature open protocol is the right tool

**The filter question:** *Does this feature make the PartyBox a better WiFi speaker in a way that Spotify Connect, AirPlay, or Bluetooth AVRCP cannot?* If the answer is no, it does not belong in the SDK.

## Consequences

**Benefits:**
- The SDK surface area stays small and focused. Each implemented feature has a clear reason to exist.
- No duplicated effort: media control through the proprietary protocol would be a worse version of what librespot and shairport-sync already provide.
- Clearer mental model for contributors: the SDK is for hardware features, not for controlling playback.

**Accepted trade-offs:**
- The SDK cannot control volume on behalf of a caller who is not using Spotify Connect or AirPlay (e.g. when the input source is AUX). This is an acceptable gap for the v1.0 release.
- Input source selection is in scope but requires understanding when to use it — the companion must switch sources correctly as streaming services start and stop.

**Future path:** Volume and input source control via the proprietary protocol may be added post-v1.0 for users who want direct hardware control independent of any streaming service.
