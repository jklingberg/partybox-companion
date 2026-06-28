"""Unit tests for VolumeState and SpotifyService volume inference."""

from __future__ import annotations

from companion.config import SpotifySettings
from companion.services.spotify import SpotifyService
from companion.volume import VolumeState

# ---------------------------------------------------------------------------
# VolumeState
# ---------------------------------------------------------------------------


def test_initial_level_is_none() -> None:
    vs = VolumeState()
    assert vs.level is None


def test_update_sets_level() -> None:
    vs = VolumeState()
    vs.update(75)
    assert vs.level == 75


def test_update_overwrites_previous() -> None:
    vs = VolumeState()
    vs.update(50)
    vs.update(25)
    assert vs.level == 25


def test_update_boundary_min() -> None:
    vs = VolumeState()
    vs.update(0)
    assert vs.level == 0


def test_update_boundary_max() -> None:
    vs = VolumeState()
    vs.update(100)
    assert vs.level == 100


# ---------------------------------------------------------------------------
# SpotifyService — volume inference from librespot stderr
# ---------------------------------------------------------------------------


def _service_with_state() -> tuple[SpotifyService, VolumeState]:
    vs = VolumeState()
    svc = SpotifyService(SpotifySettings(), volume_state=vs)
    return svc, vs


def test_volume_inferred_from_librespot_stderr() -> None:
    svc, vs = _service_with_state()
    svc._infer_volume("mixer: set volume to 65535 (100%)")
    assert vs.level == 100


def test_volume_inferred_at_midpoint() -> None:
    svc, vs = _service_with_state()
    svc._infer_volume("mixer: set volume to 32767 (50%)")
    assert vs.level == 50


def test_volume_inferred_case_insensitive() -> None:
    svc, vs = _service_with_state()
    svc._infer_volume("MIXER: SET VOLUME TO 0 (0%)")
    assert vs.level == 0


def test_volume_not_inferred_from_irrelevant_line() -> None:
    svc, vs = _service_with_state()
    svc._infer_volume("track is now playing (50 seconds in)")
    assert vs.level is None


def test_volume_inference_without_volume_state_is_noop() -> None:
    svc = SpotifyService(SpotifySettings())
    svc._infer_volume("mixer: set volume to 65535 (100%)")
    # No error and no state update expected


def test_volume_state_not_required() -> None:
    svc = SpotifyService(SpotifySettings())
    assert svc.status.running is False
