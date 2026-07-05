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
OPCODE_BATTERY_REQUEST: int = 0x9D  # Request battery status (payload: feature-id list)
OPCODE_BATTERY_RESPONSE: int = 0x9E  # Response: repeating [feature-id][len][value LE]

# Power payload values (byte 3 of the 4-byte power frame AA 03 01 <value>)
POWER_VALUE_ON: int = 0x05  # Confirmed: AA 03 01 05
POWER_VALUE_OFF: int = 0x04  # Confirmed: AA 03 01 04

# Note: the PartyBox 520 exposes neither the standard BLE Battery Service
# (0x180F) nor the Device Information Service (0x180A). Battery status is read
# via the vendor protocol (opcode 0x9D → 0x9E); see BatteryCapability.
