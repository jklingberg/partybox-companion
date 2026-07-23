"""Unit tests for the PipeWire volume actuator (companion.services.pipewire_volume)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from companion.services import pipewire_volume

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_proc(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


# ---------------------------------------------------------------------------
# set_volume()
# ---------------------------------------------------------------------------


async def test_set_volume_returns_true_on_success() -> None:
    with patch(
        "companion.services.pipewire_volume.asyncio.create_subprocess_exec",
        return_value=_mock_proc(),
    ):
        assert await pipewire_volume.set_volume(50) is True


async def test_set_volume_passes_default_sink_and_fraction() -> None:
    exec_mock = AsyncMock(return_value=_mock_proc())
    with patch("companion.services.pipewire_volume.asyncio.create_subprocess_exec", exec_mock):
        await pipewire_volume.set_volume(50)
    exec_mock.assert_awaited_once_with(
        "wpctl",
        "set-volume",
        "@DEFAULT_AUDIO_SINK@",
        "0.50",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )


async def test_set_volume_100_percent_maps_to_full_fraction() -> None:
    exec_mock = AsyncMock(return_value=_mock_proc())
    with patch("companion.services.pipewire_volume.asyncio.create_subprocess_exec", exec_mock):
        await pipewire_volume.set_volume(100)
    args = exec_mock.await_args.args
    assert args[3] == "1.00"


async def test_set_volume_returns_false_on_nonzero_exit() -> None:
    with patch(
        "companion.services.pipewire_volume.asyncio.create_subprocess_exec",
        return_value=_mock_proc(returncode=1, stderr=b"no default sink"),
    ):
        assert await pipewire_volume.set_volume(50) is False


async def test_set_volume_returns_false_when_wpctl_missing() -> None:
    with patch(
        "companion.services.pipewire_volume.asyncio.create_subprocess_exec",
        side_effect=OSError("wpctl not found"),
    ):
        assert await pipewire_volume.set_volume(50) is False


async def test_set_volume_returns_false_on_timeout() -> None:
    proc = MagicMock()
    proc.communicate = AsyncMock(side_effect=TimeoutError)
    with patch(
        "companion.services.pipewire_volume.asyncio.create_subprocess_exec",
        return_value=proc,
    ):
        assert await pipewire_volume.set_volume(50) is False


@pytest.mark.parametrize("percent", [-1, 101])
async def test_set_volume_rejects_out_of_range(percent: int) -> None:
    with pytest.raises(ValueError):
        await pipewire_volume.set_volume(percent)


@pytest.mark.parametrize("percent", [0, 100])
async def test_set_volume_accepts_boundary_values(percent: int) -> None:
    with patch(
        "companion.services.pipewire_volume.asyncio.create_subprocess_exec",
        return_value=_mock_proc(),
    ):
        assert await pipewire_volume.set_volume(percent) is True


# ---------------------------------------------------------------------------
# get_volume()
# ---------------------------------------------------------------------------


async def test_get_volume_parses_wpctl_output() -> None:
    with patch(
        "companion.services.pipewire_volume.asyncio.create_subprocess_exec",
        return_value=_mock_proc(stdout=b"Volume: 0.53\n"),
    ):
        assert await pipewire_volume.get_volume() == 53


async def test_get_volume_parses_muted_suffix() -> None:
    with patch(
        "companion.services.pipewire_volume.asyncio.create_subprocess_exec",
        return_value=_mock_proc(stdout=b"Volume: 0.75 [MUTED]\n"),
    ):
        assert await pipewire_volume.get_volume() == 75


async def test_get_volume_rounds_to_nearest_percent() -> None:
    with patch(
        "companion.services.pipewire_volume.asyncio.create_subprocess_exec",
        return_value=_mock_proc(stdout=b"Volume: 1.00\n"),
    ):
        assert await pipewire_volume.get_volume() == 100


async def test_get_volume_clamps_boosted_volume_above_100() -> None:
    """WirePlumber allows boosted volume above 1.0 (e.g. a manual wpctl
    set-volume ... 1.2 outside this module's control) — get_volume() must
    still honor its documented 0-100 contract."""
    with patch(
        "companion.services.pipewire_volume.asyncio.create_subprocess_exec",
        return_value=_mock_proc(stdout=b"Volume: 1.20\n"),
    ):
        assert await pipewire_volume.get_volume() == 100


async def test_get_volume_returns_none_on_nonzero_exit() -> None:
    with patch(
        "companion.services.pipewire_volume.asyncio.create_subprocess_exec",
        return_value=_mock_proc(returncode=1),
    ):
        assert await pipewire_volume.get_volume() is None


async def test_get_volume_returns_none_when_wpctl_missing() -> None:
    with patch(
        "companion.services.pipewire_volume.asyncio.create_subprocess_exec",
        side_effect=OSError("wpctl not found"),
    ):
        assert await pipewire_volume.get_volume() is None


async def test_get_volume_returns_none_on_timeout() -> None:
    proc = MagicMock()
    proc.communicate = AsyncMock(side_effect=TimeoutError)
    with patch(
        "companion.services.pipewire_volume.asyncio.create_subprocess_exec",
        return_value=proc,
    ):
        assert await pipewire_volume.get_volume() is None


async def test_get_volume_returns_none_on_unparseable_output() -> None:
    with patch(
        "companion.services.pipewire_volume.asyncio.create_subprocess_exec",
        return_value=_mock_proc(stdout=b"no default sink found\n"),
    ):
        assert await pipewire_volume.get_volume() is None


# ---------------------------------------------------------------------------
# pin_sink_volume() — ARCH-04/INC-2: pin the sink after an A2DP reconnect
# ---------------------------------------------------------------------------


async def test_pin_sink_volume_pins_to_100_when_no_level_known() -> None:
    """A true fresh boot/pairing — nothing recorded yet — still targets 100%,
    the INC-2 symptom this actuator exists to fix."""
    with patch(
        "companion.services.pipewire_volume.set_volume", return_value=True
    ) as mock_set_volume:
        await pipewire_volume.pin_sink_volume(None)
    mock_set_volume.assert_awaited_once_with(100)


async def test_pin_sink_volume_pins_to_last_known_level_on_reconnect() -> None:
    """A routine reconnect after the user already set a level must re-apply
    that level, not slam the sink back to 100%."""
    with patch(
        "companion.services.pipewire_volume.set_volume", return_value=True
    ) as mock_set_volume:
        await pipewire_volume.pin_sink_volume(20)
    mock_set_volume.assert_awaited_once_with(20)
