"""Unit tests for the post-A2DP-connect volume wiring.

Covers ``_pin_volume_stages`` in ``companion.__main__`` — the seam that runs the
two controllable stages of INC-2's volume chain on a fresh connect — and the
``AudioSettings.amp_baseline`` field that feeds it. The individual actuators are
covered by test_pipewire_volume.py and test_avrcp_volume.py; what is tested here
is the ordering and skip behaviour between them, plus that the configured
baseline actually reaches the actuator.
"""

from __future__ import annotations

import pytest
from companion.__main__ import _pin_volume_stages
from companion.config import AudioSettings, CompanionSettings
from companion.services.audio import AudioService
from companion.volume import VolumeState
from pydantic import ValidationError

_ADDRESS = "50:1B:6A:14:FD:1D"


class _Recorder:
    """Records calls to both actuators in order, so ordering can be asserted."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def pin_sink(self, level: int | None) -> None:
        self.calls.append(("sink", level))

    async def raise_amp(self, address: str, baseline: int) -> None:
        self.calls.append(("amp", (address, baseline)))


def _audio(address: str | None = _ADDRESS) -> AudioService:
    """A real AudioService, so `status.address` reflects the real property."""
    return AudioService(AudioSettings(sink_address=address))


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    import companion.__main__ as main_mod

    recorder = _Recorder()
    monkeypatch.setattr(main_mod.pipewire_volume, "pin_sink_volume", recorder.pin_sink)
    monkeypatch.setattr(main_mod.avrcp_volume, "raise_amp_to_baseline", recorder.raise_amp)
    return recorder


# ---------------------------------------------------------------------------
# _pin_volume_stages()
# ---------------------------------------------------------------------------


async def test_pins_sink_before_amplifier(rec: _Recorder) -> None:
    """Stage 2 first: it is the cheap local call and the one INC-2 was about."""
    await _pin_volume_stages(_audio(), VolumeState(), 52)
    assert [name for name, _ in rec.calls] == ["sink", "amp"]


async def test_passes_configured_baseline_to_the_amplifier(rec: _Recorder) -> None:
    await _pin_volume_stages(_audio(), VolumeState(), 96)
    assert ("amp", (_ADDRESS, 96)) in rec.calls


async def test_passes_recorded_level_to_the_sink(rec: _Recorder) -> None:
    """pin_sink_volume targets the last known level, not a hardcoded 100."""
    state = VolumeState()
    state.level = 40
    await _pin_volume_stages(_audio(), state, 52)
    assert ("sink", 40) in rec.calls


async def test_sink_still_pinned_when_address_unknown(rec: _Recorder) -> None:
    """No address means no AVRCP target, but stage 2 must not be skipped."""
    await _pin_volume_stages(_audio(address=None), VolumeState(), 52)
    assert [name for name, _ in rec.calls] == ["sink"]


async def test_amplifier_skipped_when_address_unknown(rec: _Recorder) -> None:
    await _pin_volume_stages(_audio(address=None), VolumeState(), 52)
    assert not any(name == "amp" for name, _ in rec.calls)


# ---------------------------------------------------------------------------
# AudioSettings.amp_baseline
# ---------------------------------------------------------------------------


def test_amp_baseline_defaults_to_the_calibrated_value() -> None:
    """52 was calibrated by ear at both slider extremes (2026-08-12)."""
    assert AudioSettings().amp_baseline == 52


@pytest.mark.parametrize("bad", [-1, 128, 1000])
def test_amp_baseline_rejects_values_outside_the_avrcp_scale(bad: int) -> None:
    with pytest.raises(ValidationError):
        AudioSettings(amp_baseline=bad)


@pytest.mark.parametrize("ok", [0, 52, 127])
def test_amp_baseline_accepts_the_whole_avrcp_scale(ok: int) -> None:
    assert AudioSettings(amp_baseline=ok).amp_baseline == ok


def test_amp_baseline_survives_the_sink_address_override() -> None:
    """Regression: __main__ used to rebuild AudioSettings field by field, which
    silently dropped any field it did not name. model_copy keeps them."""
    configured = AudioSettings(sink_address="AA:BB:CC:DD:EE:FF", amp_baseline=96)
    overridden = configured.model_copy(update={"sink_address": _ADDRESS})
    assert overridden.sink_address == _ADDRESS
    assert overridden.amp_baseline == 96


def test_amp_baseline_reaches_audio_settings_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMPANION_AUDIO__AMP_BASELINE", "77")
    assert CompanionSettings().audio.amp_baseline == 77
