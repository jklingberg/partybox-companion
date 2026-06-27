"""Protocol constants for the JBL PartyBox vendor control protocol.

All findings documented here are confirmed from real hardware captures. See
docs/reverse-engineering/discoveries.md for the evidence trail. Speculative
values are never added here — only confirmed ones.
"""

# Vendor frame constants (excelpoint.com control service, TX/RX characteristics)
SOF: int = 0xAA  # Start-of-frame byte for all vendor commands

# Opcodes (byte 1 of every vendor frame, immediately after SOF)
OPCODE_POWER: int = 0x03  # Power on / off
OPCODE_FIRMWARE_REQUEST: int = 0x21  # Request firmware version (no payload)
OPCODE_FIRMWARE_RESPONSE: int = 0x22  # Response: [major][minor][patch][0x00]

# Power payload values (byte 3 of the 4-byte power frame AA 03 01 <value>)
POWER_VALUE_ON: int = 0x05  # Confirmed: AA 03 01 05
POWER_VALUE_OFF: int = 0x04  # Confirmed: AA 03 01 04

# ---------------------------------------------------------------------------
# Standard BLE Battery Service
# Used by BatteryCapability on models that expose the Battery Service
# (portable models only — the PartyBox 520 is mains-powered and has no
# battery service). The standard Device Information Service (0x180A) is
# absent on the 520; device info goes through the vendor protocol instead.
# ---------------------------------------------------------------------------

BATTERY_SERVICE_UUID: str = "0000180f-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_CHAR_UUID: str = "00002a19-0000-1000-8000-00805f9b34fb"
