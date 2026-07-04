# ADR-030 — BlueZ GATT Configuration: Disable EATT, Enable AutoEnable

**Status:** Accepted
**Date:** 2026-07-04
**Milestone:** M13 (documented retroactively during M18 validation)

---

## Context

Speaker control is BLE GATT over the vendor service `65786365-6c70-6f69-6e74-2e636f6d0000` ("excelpoint.com"; see [ADR-015](015-bluetooth-control-transport.md)). During M13 hardware validation, GATT connections to the JBL PartyBox 520 failed intermittently with `bleak` reporting `GATT Protocol Error: Unlikely Error`. The decision (`Channels = 1` in `/etc/bluetooth/main.conf`) already ships — applied by `image/install.sh` with a short inline comment — but the diagnostic reasoning behind it lived only in developer notes, not in a versioned decision record. A load-bearing config line with no recorded rationale is the kind of thing a future contributor "cleans up" without knowing why it is there; this ADR records the why.

**Root cause of "Unlikely Error" — EATT.** The PartyBox 520 advertises Enhanced ATT support (GATT *Server Supported Features* = `0x01`). Seeing this, BlueZ opens an Enhanced Credit-Based (EATT) L2CAP channel on PSM `0x0027` before GATT operations. The speaker refuses EATT without an encrypted LE link (`All Connections refused - insufficient authentication`). BlueZ then auto-initiates SMP pairing to establish encryption; the speaker rejects it (`Pairing not supported`, reason `0x05`); BlueZ tears the link down. That teardown surfaces to `bleak` as the opaque "Unlikely Error".

Crucially, the vendor service works fine over the **standard** ATT bearer (CID `0x0004`) with no encryption at all — only the EATT path demanded encryption the speaker would not provide. LE SMP pairing (ordinary *or* bonding) fails against this speaker regardless; it simply does not accept LE pairing.

Separately, the Bluetooth adapter came up `DOWN` at boot on the base image, which had to be corrected before any Bluetooth work could proceed.

---

## Decision

Configure BlueZ via `/etc/bluetooth/main.conf` (applied by `image/install.sh`):

```ini
[Policy]
AutoEnable=true

[GATT]
Channels = 1
```

- **`Channels = 1`** restricts BlueZ to a single ATT bearer — the standard unencrypted ATT channel — disabling EATT entirely. This removes the PSM `0x0027` negotiation, the failed SMP escalation, and therefore the "Unlikely Error". The vendor control service does not need or use EATT.
- **`AutoEnable=true`** powers the adapter on at boot so the appliance does not require a manual `hciconfig hci0 up`.

This is orthogonal to the **A2DP BR/EDR** bond ([ADR-027](027-bluetooth-bonding-architecture.md)): that bond is Bluetooth Classic Secure Simple Pairing for audio, established in a scoped bondable window. The GATT control link here is unencrypted BLE ATT and is *not* bonded — the two Bluetooth subsystems are independent.

---

## Alternatives considered

### Bond over LE (SMP) to satisfy EATT's encryption requirement

The "intended" path — establish an encrypted LE link so EATT is accepted. Rejected: the speaker rejects LE SMP pairing outright (`Pairing not supported`), so there is no way to obtain the encrypted link EATT wants. Even if there were, the control service works over plain ATT, so the encryption would be pure overhead.

### Leave EATT enabled and retry on "Unlikely Error"

Retrying does not help — the failure is deterministic for this speaker, not transient. It would add reconnect latency and log noise for a condition that a one-line config change eliminates outright.

### Set `AlwaysPairable` / bond the control link

Unnecessary and undesirable: the control link needs no encryption, and a permanently pairable adapter is a security concern for an appliance used near strangers' devices (the same reasoning as [ADR-027](027-bluetooth-bonding-architecture.md)'s scoped bondable mode).

---

## Consequences

- BLE GATT control connections to the PartyBox 520 are reliable; the "Unlikely Error" does not occur.
- The appliance depends on a single, well-understood BlueZ config file, applied declaratively at image build.
- **Risk / revisit trigger:** if a future speaker model *requires* EATT (or requires an encrypted control link), `Channels = 1` would prevent connection and this decision must be revisited — likely alongside LE bonding support in the SDK. The symptom to watch for is a return of "Unlikely Error" or `insufficient authentication` on GATT connect against a new model.
- Disabling EATT is host-wide, but the appliance connects to exactly one speaker over one vendor service, so there is no other GATT peer whose performance the single-bearer restriction could affect.
