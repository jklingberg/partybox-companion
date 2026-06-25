# Model Support Strategy

## The problem

The JBL PartyBox line spans a wide range of models — from the compact PartyBox 110 to the large PartyBox 1000. Not all models support the same features. Some have LED lighting arrays; others do not. Some have EQ control; others do not. Battery status is only relevant on portable models.

Hardcoding per-model behaviour produces a maintenance nightmare: every new model requires code changes, and per-model branches interact unpredictably.

## The solution: capability-based design

Rather than branching on model, the `partybox` SDK exposes capabilities as typed optional properties on the `Device` object. A capability is `None` if the connected device does not support it.

```python
speaker = await PartyBox.discover()

# Required capabilities — present on every device
await speaker.power.turn_on()
print(await speaker.device_info.firmware_version())

# Optional capabilities — None if unsupported by this model
if speaker.battery is not None:
    level = await speaker.battery.level()

# Post-v1.0 optional capabilities follow the same pattern
if speaker.lights is not None:
    await speaker.lights.set_mode(LightMode.PULSE)

if speaker.microphone is not None:
    await speaker.microphone.mute()
```

Callers check for `None` once. No model names appear in business logic.

## How capabilities are detected

At connection time, `PartyBoxDevice` probes the device via protocol queries and populates the capability registry. Capabilities not reported by the device are `None`. The probing mechanism is documented in `docs/reverse-engineering/protocol.md` as it is confirmed.

This is preferable to a static model→capabilities map because:
- It works with firmware updates that add or remove features
- It works with models not yet tested
- It does not require maintaining a model database

## Adding support for a new model

Support for a new model is generally automatic: connect a device, observe which capabilities it reports, and confirm the behaviour. If the new model uses the same protocol with the same capability set as an existing model, no code changes are needed.

If the new model adds a previously unseen capability:

1. Capture the relevant traffic (see `docs/reverse-engineering/guide.md`)
2. Document the protocol bytes in `docs/reverse-engineering/protocol.md`
3. Add the message dataclasses in `packages/partybox/src/partybox/protocol/messages.py`
4. Create `packages/partybox/src/partybox/device/capabilities/<name>.py`
5. Add the optional property to the `Device` ABC and `PartyBoxDevice`
6. Add the model to the supported hardware table below and in the protocol doc

Nothing else changes. Existing code continues working unchanged.

## Supported hardware

| Model | Power | DeviceInfo | Battery | Lights | EQ | Microphone | Notes |
|---|---|---|---|---|---|---|---|
| PartyBox 520 | TBD | TBD | ❌ | TBD | TBD | TBD | Primary test device |

Legend: ✅ confirmed · ❌ not present · TBD not yet tested · ❓ unclear

## Firmware compatibility

Assume JBL will continue releasing firmware updates. Where a firmware version affects protocol behaviour, document it in `docs/reverse-engineering/discoveries.md` with the firmware version that introduced the change.

Do not add firmware-version branches in production code unless there is absolutely no other option.

## Models not listed here

If you have a PartyBox model not in the table above, your captures are valuable. See `docs/reverse-engineering/guide.md` for how to capture and contribute.
