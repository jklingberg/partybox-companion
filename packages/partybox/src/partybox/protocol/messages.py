"""Typed message dataclasses for the PartyBox vendor protocol.

Each message type maps to one logical command or notification. The codec
(``partybox.protocol.codec``) converts between message objects and raw bytes.

Only messages whose bytes are confirmed from hardware captures are defined here.
See docs/reverse-engineering/discoveries.md for the evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class PowerState(IntEnum):
    """Power state payload values, confirmed from hardware captures."""

    ON = 0x05  # AA 03 01 05
    OFF = 0x04  # AA 03 01 04


@dataclass(frozen=True)
class PowerCommand:
    """Command to set the speaker's power state.

    Args:
        state: :attr:`PowerState.ON` or :attr:`PowerState.OFF`.
    """

    state: PowerState


@dataclass(frozen=True)
class FirmwareVersionRequest:
    """Request the speaker's firmware version.

    Encodes to ``AA 21 00``. The speaker responds with a
    :class:`FirmwareVersionResponse` notification.
    """


@dataclass(frozen=True)
class FirmwareVersionResponse:
    """Firmware version notification from the speaker.

    Decoded from ``AA 22 04 [major] [minor] [patch] [0x00]``.
    Confirmed from a JBL PartyBox 520 running firmware 26.2.10.
    """

    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


class BatteryFeature(IntEnum):
    """Feature ids selectable in a battery-status request/response.

    Confirmed from the JBL app (``BatteryInfo.FeatureType``) and hardware
    captures on a PartyBox 520. Each id maps to one TLV field in the response.
    """

    BATTERY_ID = 1  # ASCII part/serial string, not a number
    REMAINING_PLAYTIME = 2  # minutes; 0xFFFF sentinel when not discharging
    TEMPERATURE_MAX = 3
    REMAINING_CAPACITY = 4  # mAh
    FULL_CHARGE_CAPACITY = 5  # mAh
    DESIGN_CAPACITY = 6  # mAh
    CYCLE_COUNT = 7
    STATE_OF_HEALTH = 8  # percent
    CHARGING_STATUS = 9  # see ChargingStatus
    BATTERY_HEALTH_NOTIFICATION = 10
    TOTAL_POWER_ON_DURATION = 11  # minutes
    TOTAL_PLAYBACK_TIME_DURATION = 12  # minutes


class ChargingStatus(IntEnum):
    """Charge state, from the ``CHARGING_STATUS`` battery feature.

    Confirmed values on a PartyBox 520 (the app also exposes ``CHARGING`` /
    ``FULL`` labels):

    * ``1`` on mains while charging (observed at 91 %),
    * ``2`` on battery / discharging,
    * ``3`` on mains once fully charged (observed at 100 %).

    ``CHARGING`` and ``FULL`` both mean the speaker is on wall power. Unknown
    values decode to ``None``.
    """

    CHARGING = 1  # on wall power, battery charging
    DISCHARGING = 2  # running on battery
    FULL = 3  # on wall power, fully charged

    @property
    def on_mains(self) -> bool:
        """Whether the speaker is on wall power (charging or full)."""
        return self in (ChargingStatus.CHARGING, ChargingStatus.FULL)


# Sentinel the speaker reports for REMAINING_PLAYTIME when it is not meaningful
# (e.g. on mains power). Surfaced as ``None`` rather than 65535.
PLAYTIME_UNKNOWN = 0xFFFF


@dataclass(frozen=True)
class BatteryStatusRequest:
    """Request the speaker's battery status.

    Encodes to ``AA 9D <n> <feature-id…>``. ``features`` is the list of
    :class:`BatteryFeature` ids to request; defaults to all known features.
    """

    features: tuple[BatteryFeature, ...] = tuple(BatteryFeature)


@dataclass(frozen=True)
class BatteryStatusResponse:
    """Battery-status notification from the speaker (opcode ``0x9E``).

    Decoded from a repeating TLV payload ``[feature-id][len][value LE]``.
    Unrequested/absent fields are ``None``. Confirmed from PartyBox 520
    captures on both battery and mains power.
    """

    battery_id: str | None = None
    remaining_playtime_minutes: int | None = None
    temperature_max: int | None = None
    remaining_capacity_mah: int | None = None
    full_charge_capacity_mah: int | None = None
    design_capacity_mah: int | None = None
    cycle_count: int | None = None
    state_of_health_percent: int | None = None
    charging_status: ChargingStatus | None = None
    total_power_on_minutes: int | None = None
    total_playback_minutes: int | None = None

    @property
    def charge_percent(self) -> int | None:
        """Derived charge level (0-100).

        The speaker reports no direct percentage; it is computed from
        ``remaining_capacity / full_charge_capacity``. ``None`` if either
        capacity is unavailable.
        """
        if self.remaining_capacity_mah is None or not self.full_charge_capacity_mah:
            return None
        pct = round(100 * self.remaining_capacity_mah / self.full_charge_capacity_mah)
        return max(0, min(100, pct))
