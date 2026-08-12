"""Unit tests for the AVRCP amplifier actuator (companion.services.avrcp_volume)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from companion.services import avrcp_volume

_ADDRESS = "50:1B:6A:14:FD:1D"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_proc(stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


# ---------------------------------------------------------------------------
# get_amp_volume()
# ---------------------------------------------------------------------------


async def test_get_amp_volume_parses_helper_output() -> None:
    with patch.object(avrcp_volume, "_run_helper", AsyncMock(return_value="52")):
        assert await avrcp_volume.get_amp_volume(_ADDRESS) == 52


async def test_get_amp_volume_asks_the_helper_for_volume() -> None:
    helper = AsyncMock(return_value="52")
    with patch.object(avrcp_volume, "_run_helper", helper):
        await avrcp_volume.get_amp_volume(_ADDRESS)
    helper.assert_awaited_once_with(_ADDRESS, "volume")


async def test_get_amp_volume_none_when_no_transport() -> None:
    """ "none" covers link-down and speakers with no absolute volume alike."""
    with patch.object(avrcp_volume, "_run_helper", AsyncMock(return_value="none")):
        assert await avrcp_volume.get_amp_volume(_ADDRESS) is None


async def test_get_amp_volume_none_on_unparseable_output() -> None:
    with patch.object(avrcp_volume, "_run_helper", AsyncMock(return_value="err:boom")):
        assert await avrcp_volume.get_amp_volume(_ADDRESS) is None


async def test_get_amp_volume_none_when_out_of_range() -> None:
    """A level above the AVRCP scale means BlueZ told us something impossible."""
    with patch.object(avrcp_volume, "_run_helper", AsyncMock(return_value="200")):
        assert await avrcp_volume.get_amp_volume(_ADDRESS) is None


async def test_get_amp_volume_zero_is_not_confused_with_none() -> None:
    """A genuinely muted amplifier is 0, distinct from "nothing to report"."""
    with patch.object(avrcp_volume, "_run_helper", AsyncMock(return_value="0")):
        assert await avrcp_volume.get_amp_volume(_ADDRESS) == 0


# ---------------------------------------------------------------------------
# set_amp_volume()
# ---------------------------------------------------------------------------


async def test_set_amp_volume_returns_true_on_ok() -> None:
    with patch.object(avrcp_volume, "_run_helper", AsyncMock(return_value="ok")):
        assert await avrcp_volume.set_amp_volume(_ADDRESS, 64) is True


async def test_set_amp_volume_encodes_level_in_command() -> None:
    helper = AsyncMock(return_value="ok")
    with patch.object(avrcp_volume, "_run_helper", helper):
        await avrcp_volume.set_amp_volume(_ADDRESS, 64)
    helper.assert_awaited_once_with(_ADDRESS, "volume=64")


async def test_set_amp_volume_returns_false_on_helper_error() -> None:
    with patch.object(avrcp_volume, "_run_helper", AsyncMock(return_value="err:no A2DP transport")):
        assert await avrcp_volume.set_amp_volume(_ADDRESS, 64) is False


async def test_set_amp_volume_returns_false_when_helper_unavailable() -> None:
    with patch.object(avrcp_volume, "_run_helper", AsyncMock(return_value=None)):
        assert await avrcp_volume.set_amp_volume(_ADDRESS, 64) is False


@pytest.mark.parametrize("level", [-1, 128, 1000])
async def test_set_amp_volume_rejects_out_of_scale_levels(level: int) -> None:
    with pytest.raises(ValueError, match="0-127"):
        await avrcp_volume.set_amp_volume(_ADDRESS, level)


# ---------------------------------------------------------------------------
# raise_amp_to_baseline() — INC-2 stage 3
# ---------------------------------------------------------------------------


async def test_raise_amp_to_baseline_raises_when_below() -> None:
    """The INC-2 case: the speaker remembered 40/127 (31%) after a fresh flash."""
    setter = AsyncMock(return_value=True)
    with (
        patch.object(avrcp_volume, "get_amp_volume", AsyncMock(return_value=40)),
        patch.object(avrcp_volume, "set_amp_volume", setter),
    ):
        await avrcp_volume.raise_amp_to_baseline(_ADDRESS, 64)
    setter.assert_awaited_once_with(_ADDRESS, 64)


async def test_raise_amp_to_baseline_never_lowers() -> None:
    """A floor, not a pin: the speaker's knob is a legitimate authority (ADR-022).

    Lowering here would fight the operator audibly on every A2DP reconnect.
    """
    setter = AsyncMock(return_value=True)
    with (
        patch.object(avrcp_volume, "get_amp_volume", AsyncMock(return_value=100)),
        patch.object(avrcp_volume, "set_amp_volume", setter),
    ):
        await avrcp_volume.raise_amp_to_baseline(_ADDRESS, 64)
    setter.assert_not_awaited()


async def test_raise_amp_to_baseline_writes_when_read_equals_baseline() -> None:
    """A read equal to the baseline is untrustworthy, so it must still write.

    BlueZ's Volume is a cache and the baseline is the only value this function
    writes, so "already at the baseline" is exactly what a stale cache looks like.
    Observed on hardware 2026-08-12: knob at 20, BlueZ reporting 52, the floor
    skipping, and the amplifier audibly stuck at 20 for the whole session.
    """
    setter = AsyncMock(return_value=True)
    with (
        patch.object(avrcp_volume, "get_amp_volume", AsyncMock(return_value=64)),
        patch.object(avrcp_volume, "set_amp_volume", setter),
    ):
        await avrcp_volume.raise_amp_to_baseline(_ADDRESS, 64)
    setter.assert_awaited_once_with(_ADDRESS, 64)


@pytest.fixture
def no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the retry backoff so the None paths do not really sleep."""
    monkeypatch.setattr(avrcp_volume, "_VOLUME_WAIT_DELAY", 0)


async def test_raise_amp_to_baseline_no_write_when_level_unknown(no_wait: None) -> None:
    """No reported level means no absolute volume to pin — leave the speaker alone."""
    setter = AsyncMock(return_value=True)
    with (
        patch.object(avrcp_volume, "get_amp_volume", AsyncMock(return_value=None)),
        patch.object(avrcp_volume, "set_amp_volume", setter),
    ):
        await avrcp_volume.raise_amp_to_baseline(_ADDRESS, 64)
    setter.assert_not_awaited()


async def test_raise_amp_to_baseline_retries_while_volume_absent(no_wait: None) -> None:
    """BlueZ can expose MediaTransport1.Volume after the transport itself.

    Reading once raced that window: losing it left the amplifier unraised for the
    whole session, silently reproducing INC-2.
    """
    getter = AsyncMock(side_effect=[None, None, 40])
    setter = AsyncMock(return_value=True)
    with (
        patch.object(avrcp_volume, "get_amp_volume", getter),
        patch.object(avrcp_volume, "set_amp_volume", setter),
    ):
        await avrcp_volume.raise_amp_to_baseline(_ADDRESS, 52)
    assert getter.await_count == 3
    setter.assert_awaited_once_with(_ADDRESS, 52)


async def test_raise_amp_to_baseline_gives_up_after_bounded_attempts(no_wait: None) -> None:
    """Bounded: a speaker with no absolute volume never grows the property."""
    getter = AsyncMock(return_value=None)
    setter = AsyncMock(return_value=True)
    with (
        patch.object(avrcp_volume, "get_amp_volume", getter),
        patch.object(avrcp_volume, "set_amp_volume", setter),
    ):
        await avrcp_volume.raise_amp_to_baseline(_ADDRESS, 52)
    assert getter.await_count == avrcp_volume._VOLUME_WAIT_ATTEMPTS
    setter.assert_not_awaited()


async def test_raise_amp_to_baseline_reads_once_when_volume_already_present() -> None:
    """The common case must not pay for the retry, nor sleep at all."""
    getter = AsyncMock(return_value=40)
    with (
        patch.object(avrcp_volume, "get_amp_volume", getter),
        patch.object(avrcp_volume, "set_amp_volume", AsyncMock(return_value=True)),
    ):
        await avrcp_volume.raise_amp_to_baseline(_ADDRESS, 52)
    assert getter.await_count == 1


async def test_raise_amp_to_baseline_no_retry_when_already_above_baseline() -> None:
    """An at-or-above reading is a definitive answer — do not keep polling."""
    getter = AsyncMock(return_value=100)
    setter = AsyncMock(return_value=True)
    with (
        patch.object(avrcp_volume, "get_amp_volume", getter),
        patch.object(avrcp_volume, "set_amp_volume", setter),
    ):
        await avrcp_volume.raise_amp_to_baseline(_ADDRESS, 52)
    assert getter.await_count == 1
    setter.assert_not_awaited()


async def test_raise_amp_to_baseline_tolerates_failed_write() -> None:
    """Best-effort: a rejected write must not propagate into the connect loop."""
    with (
        patch.object(avrcp_volume, "get_amp_volume", AsyncMock(return_value=40)),
        patch.object(avrcp_volume, "set_amp_volume", AsyncMock(return_value=False)),
    ):
        await avrcp_volume.raise_amp_to_baseline(_ADDRESS, 64)


@pytest.mark.parametrize("baseline", [-1, 128])
async def test_raise_amp_to_baseline_rejects_out_of_scale_baseline(baseline: int) -> None:
    with pytest.raises(ValueError, match="0-127"):
        await avrcp_volume.raise_amp_to_baseline(_ADDRESS, baseline)


# ---------------------------------------------------------------------------
# _run_helper() — subprocess plumbing
# ---------------------------------------------------------------------------


async def test_run_helper_invokes_the_bluez_subprocess_module() -> None:
    """BlueZ calls must stay out of the loop bleak holds its own MessageBus on."""
    exec_mock = AsyncMock(return_value=_mock_proc(b"52\n"))
    with patch.object(avrcp_volume.asyncio, "create_subprocess_exec", exec_mock):
        assert await avrcp_volume._run_helper(_ADDRESS, "volume") == "52"
    args = exec_mock.await_args.args
    assert args[1:] == ("-m", "companion.services._a2dp_connect", _ADDRESS, "volume")


async def test_run_helper_none_when_subprocess_cannot_start() -> None:
    with patch.object(
        avrcp_volume.asyncio, "create_subprocess_exec", AsyncMock(side_effect=OSError("nope"))
    ):
        assert await avrcp_volume._run_helper(_ADDRESS, "volume") is None


async def test_run_helper_kills_and_returns_none_on_timeout() -> None:
    proc = _mock_proc()
    proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    proc.wait = AsyncMock(return_value=0)
    with patch.object(avrcp_volume.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)):
        assert await avrcp_volume._run_helper(_ADDRESS, "volume") is None
    proc.kill.assert_called_once()
