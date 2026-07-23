"""Tests for companion.__main__._pin_bluez_sink_volume (ARCH-04/INC-2).

See ADR-022 addendum: the pin actuator must target the last known
VolumeState level rather than unconditionally forcing 100%, so a routine
A2DP reconnect (the speaker drops the link on idle) doesn't clobber a
level the user or Spotify already set.
"""

from __future__ import annotations

from unittest.mock import patch

from companion.__main__ import _pin_bluez_sink_volume
from companion.volume import VolumeState


async def test_pins_to_100_when_no_level_known() -> None:
    """A true fresh boot/pairing — nothing recorded yet — still targets 100%,
    the INC-2 symptom this actuator exists to fix."""
    volume_state = VolumeState()
    with patch("companion.__main__.pipewire_set_volume", return_value=True) as mock_set_volume:
        await _pin_bluez_sink_volume(volume_state)
    mock_set_volume.assert_awaited_once_with(100)


async def test_pins_to_last_known_level_on_reconnect() -> None:
    """A routine reconnect after the user already set a level must re-apply
    that level, not slam the sink back to 100%."""
    volume_state = VolumeState()
    volume_state.update(20, "api")
    with patch("companion.__main__.pipewire_set_volume", return_value=True) as mock_set_volume:
        await _pin_bluez_sink_volume(volume_state)
    mock_set_volume.assert_awaited_once_with(20)
