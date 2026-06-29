"""Tests for VolumeState and SpotifyService volume inference."""

from __future__ import annotations

import pytest
from companion.volume import VolumeState

# ---------------------------------------------------------------------------
# VolumeState
# ---------------------------------------------------------------------------


def test_initial_level_is_none() -> None:
    state = VolumeState()
    assert state.level is None


def test_update_sets_level() -> None:
    state = VolumeState()
    state.update(72)
    assert state.level == 72


def test_update_overwrites_previous_level() -> None:
    state = VolumeState()
    state.update(50)
    state.update(30)
    assert state.level == 30


def test_update_accepts_zero() -> None:
    state = VolumeState()
    state.update(0)
    assert state.level == 0


def test_update_accepts_100() -> None:
    state = VolumeState()
    state.update(100)
    assert state.level == 100


def test_update_raises_for_negative() -> None:
    state = VolumeState()
    with pytest.raises(ValueError):
        state.update(-1)


def test_update_raises_above_100() -> None:
    state = VolumeState()
    with pytest.raises(ValueError):
        state.update(101)


# ---------------------------------------------------------------------------
# SpotifyService volume inference
# ---------------------------------------------------------------------------


def test_spotify_volume_inferred_from_percentage_line() -> None:
    from companion.config import SpotifySettings
    from companion.services.spotify import SpotifyService

    state = VolumeState()
    svc = SpotifyService(SpotifySettings(), volume_state=state)
    svc._infer_playback_state("Mixer: setting volume to 52428 (80%)")
    assert state.level == 80


def test_spotify_volume_inferred_case_insensitive() -> None:
    from companion.config import SpotifySettings
    from companion.services.spotify import SpotifyService

    state = VolumeState()
    svc = SpotifyService(SpotifySettings(), volume_state=state)
    svc._infer_playback_state("librespot_audio::mixer: Volume set to 32767 (50%)")
    assert state.level == 50


def test_spotify_volume_not_updated_on_irrelevant_line() -> None:
    from companion.config import SpotifySettings
    from companion.services.spotify import SpotifyService

    state = VolumeState()
    svc = SpotifyService(SpotifySettings(), volume_state=state)
    svc._infer_playback_state("Connecting to Spotify backend")
    assert state.level is None


def test_spotify_volume_without_state_does_not_raise() -> None:
    """SpotifyService works normally when no VolumeState is provided."""
    from companion.config import SpotifySettings
    from companion.services.spotify import SpotifyService

    svc = SpotifyService(SpotifySettings())  # no volume_state
    # Should not raise even when a volume line is seen
    svc._infer_playback_state("Mixer: setting volume to 65535 (100%)")


def test_spotify_volume_100_percent() -> None:
    from companion.config import SpotifySettings
    from companion.services.spotify import SpotifyService

    state = VolumeState()
    svc = SpotifyService(SpotifySettings(), volume_state=state)
    svc._infer_playback_state("volume changed to 65535 (100%)")
    assert state.level == 100


def test_spotify_volume_0_percent() -> None:
    from companion.config import SpotifySettings
    from companion.services.spotify import SpotifyService

    state = VolumeState()
    svc = SpotifyService(SpotifySettings(), volume_state=state)
    svc._infer_playback_state("volume changed to 0 (0%)")
    assert state.level == 0
