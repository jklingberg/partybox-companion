"""Tests for the stale-LE-link reclaim wrapper.

The real reclaim needs BlueZ on a system D-Bus, which CI does not have — most
of these tests cover the wrapper's stdout-parsing contract by substituting a
scripted subprocess for the real ``_le_reclaim`` helper, the same pattern
``test_adapter_recovery.py`` uses.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from companion.services import le_reclaim
from companion.services.le_reclaim import disconnect_stale_speaker_links


async def test_reclaim_returns_false_without_bluez() -> None:
    """In an environment with no system bus/BlueZ the helper subprocess
    prints an err: line; the wrapper must swallow it and report False."""
    assert await disconnect_stale_speaker_links() is False


def _script_exec(stdout: str) -> object:
    real_exec = asyncio.create_subprocess_exec

    async def scripted(*_args: object, **_kwargs: object) -> asyncio.subprocess.Process:
        return await real_exec(
            le_reclaim.sys.executable,
            "-c",
            f"print({stdout!r})",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    return scripted


async def test_reclaim_success_reports_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _script_exec("ok:3:5"))
    assert await disconnect_stale_speaker_links() is True


async def test_reclaim_nothing_found_reports_false_with_seen_count(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """count=0 but seen>0: BlueZ had matching device objects, just none
    connected — this must be distinguishable in the logs from "saw nothing
    at all" (see the 2026-07-23 outage investigation)."""
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _script_exec("ok:0:2"))
    with caplog.at_level(logging.DEBUG, logger="companion.services.le_reclaim"):
        assert await disconnect_stale_speaker_links() is False
    assert any("2 PartyBox-named" in r.message for r in caplog.records)


async def test_reclaim_nothing_seen_reports_false(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """count=0 and seen=0: BlueZ has no record of the speaker at all right
    now — distinct diagnostic message from the "seen but not connected" case."""
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _script_exec("ok:0:0"))
    with caplog.at_level(logging.DEBUG, logger="companion.services.le_reclaim"):
        assert await disconnect_stale_speaker_links() is False
    assert any("no PartyBox-named" in r.message for r in caplog.records)


async def test_reclaim_tolerates_missing_seen_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """Old-shape "ok:<n>" output (no seen count) must not break parsing."""
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _script_exec("ok:1"))
    assert await disconnect_stale_speaker_links() is True


async def test_reclaim_malformed_output_reports_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _script_exec("ok:notanumber"))
    assert await disconnect_stale_speaker_links() is False


async def test_reclaim_times_out_to_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(le_reclaim, "_RECLAIM_TIMEOUT", 0.05)

    real_exec = asyncio.create_subprocess_exec

    async def sleepy_exec(*_args: object, **_kwargs: object) -> asyncio.subprocess.Process:
        return await real_exec(
            le_reclaim.sys.executable,
            "-c",
            "import time; time.sleep(60)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", sleepy_exec)
    assert await disconnect_stale_speaker_links() is False
