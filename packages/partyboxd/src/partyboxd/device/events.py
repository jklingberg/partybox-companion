"""Device events emitted by DeviceManager and delivered to API subscribers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from partyboxd.eventbus import EventBus as EventBus


@dataclass(frozen=True)
class ConnectedEvent:
    """Emitted when the daemon establishes a connection with the speaker."""

    address: str | None
    firmware: str | None
    battery: int | None
    type: Literal["connected"] = "connected"


@dataclass(frozen=True)
class DisconnectedEvent:
    """Emitted when the speaker connection is lost or terminated."""

    type: Literal["disconnected"] = "disconnected"


@dataclass(frozen=True)
class SpeakerStateChangedEvent:
    """Emitted when the speaker's coarse power state changes.

    ``state`` mirrors :attr:`StatusSnapshot.speaker_state` — ``"off"``
    (BLE disconnected), ``"standby"`` (BLE connected, speaker asleep), or
    ``"on"`` (BLE connected, speaker awake). Distinct from
    :class:`ConnectedEvent`/:class:`DisconnectedEvent`: those track the BLE
    link itself, while this tracks the on/standby transition that can happen
    *within* an established connection (see ADR/manager.py `_poll_battery`).
    """

    state: Literal["off", "standby", "on"]
    type: Literal["speaker_state_changed"] = "speaker_state_changed"


@dataclass(frozen=True)
class PowerChangedEvent:
    """Emitted after a successful power on/off command is accepted by the speaker."""

    state: Literal["on", "off"]
    type: Literal["power_changed"] = "power_changed"


@dataclass(frozen=True)
class VolumeChangedEvent:
    """Emitted when the speaker hardware reports a volume change.

    DeviceManager emits this event when a BLE volume notification arrives
    from the speaker (hardware button press, BLE SET command confirmation).
    It is not emitted for software-side volume changes (Spotify, REST API).

    ``percent`` is normalised to 0-100 before emission; the raw BLE value
    (0-65535) is converted inside the SDK.

    .. note::
        This event is defined now to establish the notification path before
        BLE volume is implemented.  DeviceManager does not emit it yet.
        See ADR-022 for the intended volume authority model.
    """

    percent: int
    type: Literal["volume_changed"] = "volume_changed"


DeviceEvent = (
    ConnectedEvent
    | DisconnectedEvent
    | SpeakerStateChangedEvent
    | PowerChangedEvent
    | VolumeChangedEvent
)
