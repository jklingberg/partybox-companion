"""Encode and decode vendor protocol frames.

The frame format (confirmed from hardware captures):

    [ SOF=0xAA ][ opcode:u8 ][ length:u8 ][ payload:length bytes ]

No checksum is present in the observed frames. If future captures reveal one,
it will be added here. See docs/reverse-engineering/discoveries.md.

This module is **pure** — no I/O, no async, no side effects. Message objects
in, bytes out; bytes in, message objects out.
"""

from __future__ import annotations

from partybox.protocol.constants import (
    OPCODE_BATTERY_REQUEST,
    OPCODE_BATTERY_RESPONSE,
    OPCODE_FIRMWARE_REQUEST,
    OPCODE_FIRMWARE_RESPONSE,
    OPCODE_POWER,
    SOF,
)
from partybox.protocol.messages import (
    PLAYTIME_UNKNOWN,
    BatteryFeature,
    BatteryStatusRequest,
    BatteryStatusResponse,
    ChargingStatus,
    FirmwareVersionRequest,
    FirmwareVersionResponse,
    PowerCommand,
)


def encode(message: PowerCommand | FirmwareVersionRequest | BatteryStatusRequest) -> bytes:
    """Encode a command message to a vendor protocol frame.

    Args:
        message: the typed command to encode.

    Returns:
        Raw frame bytes ready to write to the TX characteristic.

    Raises:
        TypeError: if ``message`` is not a recognised command type.
    """
    if isinstance(message, PowerCommand):
        payload = bytes([message.state])
        return bytes([SOF, OPCODE_POWER, len(payload)]) + payload
    if isinstance(message, FirmwareVersionRequest):
        return bytes([SOF, OPCODE_FIRMWARE_REQUEST, 0])
    if isinstance(message, BatteryStatusRequest):
        payload = bytes(f.value for f in message.features)
        return bytes([SOF, OPCODE_BATTERY_REQUEST, len(payload)]) + payload
    raise TypeError(f"cannot encode {type(message).__name__}")


def decode(raw: bytes) -> FirmwareVersionResponse | BatteryStatusResponse | None:
    """Decode a raw notification frame into a typed response, or ``None``.

    Only response opcodes confirmed from hardware are decoded. Unknown or
    malformed frames return ``None`` so callers can skip them.

    Args:
        raw: raw bytes from the RX characteristic notification.

    Returns:
        A typed response object, or ``None`` if the frame is not recognised.
    """
    if len(raw) < 3 or raw[0] != SOF:
        return None
    opcode = raw[1]
    length = raw[2]
    payload = raw[3 : 3 + length]
    if opcode == OPCODE_FIRMWARE_RESPONSE and len(payload) >= 3:
        return FirmwareVersionResponse(major=payload[0], minor=payload[1], patch=payload[2])
    if opcode == OPCODE_BATTERY_RESPONSE:
        return _decode_battery(payload)
    return None


def _decode_battery(payload: bytes) -> BatteryStatusResponse:
    """Parse the ``0x9E`` TLV payload into a :class:`BatteryStatusResponse`.

    Layout is a sequence of ``[feature-id:1][len:1][value:len]`` fields.
    All numeric values are little-endian; ``BATTERY_ID`` is ASCII. Unknown
    feature ids and truncated fields are skipped defensively.
    """
    fields: dict[BatteryFeature, bytes] = {}
    i = 0
    while i + 2 <= len(payload):
        raw_id = payload[i]
        flen = payload[i + 1]
        value = payload[i + 2 : i + 2 + flen]
        i += 2 + flen
        if len(value) != flen:
            break  # truncated frame
        try:
            fields[BatteryFeature(raw_id)] = value
        except ValueError:
            continue  # unknown feature id — ignore

    def num(feature: BatteryFeature) -> int | None:
        raw_value = fields.get(feature)
        return int.from_bytes(raw_value, "little") if raw_value is not None else None

    battery_id_bytes = fields.get(BatteryFeature.BATTERY_ID)
    battery_id = (
        battery_id_bytes.split(b"\x00", 1)[0].decode("ascii", "replace")
        if battery_id_bytes
        else None
    )

    playtime = num(BatteryFeature.REMAINING_PLAYTIME)
    if playtime == PLAYTIME_UNKNOWN:
        playtime = None

    charging_raw = num(BatteryFeature.CHARGING_STATUS)
    try:
        charging = ChargingStatus(charging_raw) if charging_raw is not None else None
    except ValueError:
        charging = None

    return BatteryStatusResponse(
        battery_id=battery_id,
        remaining_playtime_minutes=playtime,
        temperature_max=num(BatteryFeature.TEMPERATURE_MAX),
        remaining_capacity_mah=num(BatteryFeature.REMAINING_CAPACITY),
        full_charge_capacity_mah=num(BatteryFeature.FULL_CHARGE_CAPACITY),
        design_capacity_mah=num(BatteryFeature.DESIGN_CAPACITY),
        cycle_count=num(BatteryFeature.CYCLE_COUNT),
        state_of_health_percent=num(BatteryFeature.STATE_OF_HEALTH),
        charging_status=charging,
        total_power_on_minutes=num(BatteryFeature.TOTAL_POWER_ON_DURATION),
        total_playback_minutes=num(BatteryFeature.TOTAL_PLAYBACK_TIME_DURATION),
    )
