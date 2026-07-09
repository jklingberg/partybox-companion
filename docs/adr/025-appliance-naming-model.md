# ADR-021: Appliance Naming Model — Single Identity with Optional Service Overrides

**Status:** Rejected

> **Rejected:** Companion is designed around the common case of a single
> appliance in a household, so there's no appliance identity for
> `spotify_connect_name`/future `airplay_name` to inherit from — this ADR's
> premise doesn't apply. `PortalConfig.device_name` has been removed rather
> than built out. The body below is retained as a historical record of the
> proposal.

---

## Context

The Companion Portal currently exposes two independent name fields:

- **Appliance Name** (`device_name`) — displayed in the Portal header.
- **Spotify Connect Name** (`spotify_connect_name`) — passed to librespot as `--name`, registered via Avahi mDNS.

These fields are stored independently and default to the same value (`"PartyBox"`), which implies they are related but does not enforce any relationship. Users must keep them in sync manually.

An Avahi `CollisionError` during a debugging session surfaced the underlying problem: when two librespot processes start on the same host with the same device name, mDNS registration fails silently and the service enters a retry loop. The Portal reports "Stopped" with no explanation. The incident was resolved by restarting the orphan process, but it revealed that the naming model actively contributes to confusion:

- Non-technical users do not know why there are two name fields or how they interact.
- Changing the appliance name does not change what Spotify clients see — a non-obvious gap.
- The lack of a diagnostic means collisions are invisible to the user.

As additional playback services are added (AirPlay, DLNA, Internet Radio), the current approach of adding one name field per service would multiply this confusion.

---

## Decision

This ADR captures the intended long-term naming model. **No implementation is required yet.** The current dual-field config and Portal UI are retained unchanged until a dedicated settings refactor milestone.

### Single primary identity

`device_name` is the canonical appliance identity. It is the name the user assigns to this Pi. All playback services that require a discoverable device name should inherit from `device_name` by default.

### Service-specific names as optional overrides

Each playback service that requires its own mDNS/DNS-SD registration gets a nullable name override field. `null` means "inherit from `device_name`"; an explicit string means "use this instead."

Stored config (eventual shape):

```json
{
  "device_name": "Patio",
  "spotify_connect_name": null,
  "airplay_name": null
}
```

Effective names resolved at service startup:

```
spotify effective name = spotify_connect_name ?? device_name
airplay effective name = airplay_name         ?? device_name
```

The common case — one name, consistent everywhere — requires touching exactly one field.

### Portal UX: inherit-or-override

Settings should present the default (inherit) as the primary option and expose the override as an explicit secondary choice. The exact design is deferred but the intent is:

```
Appliance name
[Patio                              ]

Spotify Connect
● Use appliance name (Patio)
○ Custom name  [                    ]
```

The two independent text fields in the current Portal are an implementation artifact, not a design intent.

### First-boot default name

The default `"PartyBox"` is a collision risk on any network with more than one Companion appliance. Before v1.0, the Portal should either:

- Prompt the user to set a name on first boot (preferred), or
- Display a persistent warning when `device_name` equals the factory default.

Silently accepting `"PartyBox"` as a steady-state name should not be the happy path.

### Collision surfaced as a named diagnostic

When a playback service fails to register its mDNS name because another process holds the same name, the Portal should surface this as a named error rather than reporting the service as "Stopped":

- **Spotify row status:** "Name conflict" (amber)
- **Detail:** "Another device named 'Patio' is already on this network"
- **Action:** "Rename" — opens Settings directly to the affected name field

This requires `SpotifyService` (and future service managers) to detect `CollisionError` in stderr and expose it as a typed field on the service status model. The resolution is user-driven (rename the appliance), not automatic.

Auto-renaming (`"Patio (2)"`) is rejected: it erodes trust, breaks saved configurations in Spotify clients, and does not survive restarts if the root cause is persistent.

---

## Consequences

**When implemented:**

- Users configure one name and see it consistently across Spotify, AirPlay, and future services.
- Renaming the appliance propagates to all services unless individually overridden.
- Advanced users (multiple appliances, per-service differentiation) retain full control via the override fields.
- Avahi collisions produce a clear, actionable diagnostic instead of a silent retry loop.

**What this does not change:**

- The backend config schema shape (`PortalConfig`) and the `GET/PUT /api/v1/config` API can evolve independently. Making `spotify_connect_name` nullable is a backwards-compatible change if the API treats an absent or null value as "inherit."
- `SpotifyService` continues to own the librespot lifecycle (ADR-016). It is the collision detector, not the resolver — the user resolves by renaming.

**Deferred questions:**

- Internet Radio and DLNA do not expose a discoverable device name in the same way as mDNS services. Whether they belong under the same inheritance model or have separate identity concepts is left for those service milestones.
- Stereo-pair and multi-room scenarios may require the override field to carry structured metadata beyond a simple name. This is out of scope for the initial naming-model implementation.

---

## Related

- [ADR-005 — Appliance over Components](005-appliance.md)
- [ADR-016 — Companion Owns the Spotify Connect Lifecycle](016-companion-owns-spotify-lifecycle.md)
- [ADR-012 — Interoperability Positioning](012-interoperability-positioning.md)
