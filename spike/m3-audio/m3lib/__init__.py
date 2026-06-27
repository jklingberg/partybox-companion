"""Shared helpers for the M3 audio-transport viability spike.

This is **exploratory spike code** (see ADR-014), not part of the ``partybox``
SDK and not expected to survive into later milestones. It exists to gather
evidence that a Raspberry Pi can act as both the BLE control endpoint and the
Bluetooth A2DP audio source for a JBL PartyBox at the same time.

The library deliberately shells out to the standard Linux tooling
(``bluetoothctl``, ``pw-dump``, ``pw-play``, ``pw-top``, ``wpctl``) rather than
wrapping BlueZ/PipeWire D-Bus directly: every step a script takes can then be
reproduced by hand on the Pi, which is exactly what a viability spike wants.
"""
