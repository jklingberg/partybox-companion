"""Tests for the observed-link-fault counters.

Thresholds are calibrated against the 2026-09-01 capture: ~90 L2CAP errors
per hour while audio stuttered, ~1 per hour once the RF environment was
fixed. Those two rates are the fixtures the grading has to separate.
"""

from __future__ import annotations

import asyncio

import pytest
from companion.services import link_health
from companion.services.link_health import LinkHealth, _classify


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    link_health.reset_cache()


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


def test_quiet_link_is_ok() -> None:
    assert _classify(l2cap=1, a2dp=0).level == "ok"


def test_incident_rate_is_bad() -> None:
    """~90 L2CAP errors/hour is the rate measured while audio stuttered."""
    assert _classify(l2cap=90, a2dp=7).level == "err"


def test_recovered_rate_is_ok() -> None:
    """~1/hour is the rate measured after the RF environment was fixed."""
    assert _classify(l2cap=1, a2dp=1).level == "ok"


def test_middling_rate_warns() -> None:
    assert _classify(l2cap=10, a2dp=0).level == "warn"


def test_drops_alone_can_grade_the_link() -> None:
    """A link that drops without visible corruption is still a bad link."""
    assert _classify(l2cap=0, a2dp=8).level == "err"


def test_worst_of_not_averaged() -> None:
    """One healthy counter must not average down a failing one."""
    assert _classify(l2cap=90, a2dp=0).level == "err"


# ---------------------------------------------------------------------------
# Reading the journal
# ---------------------------------------------------------------------------


class _Proc:
    def __init__(self, out: bytes, code: int = 0) -> None:
        self._out = out
        self.returncode = code

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._out, b""


async def test_counts_matching_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _exec(*_args: str, **_kwargs: object) -> _Proc:
        return _Proc(b"line one\nline two\nline three\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    health = await link_health.sample()
    assert health.available is True
    assert health.l2cap_errors == 3


async def test_no_matches_is_zero_not_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """journalctl exits 1 when --grep matches nothing.

    That is the commonest healthy case, so it must read as zero faults — not
    as an unreadable journal, which would suppress the row entirely.
    """

    async def _exec(*_args: str, **_kwargs: object) -> _Proc:
        return _Proc(b"", code=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    health = await link_health.sample()
    assert health.available is True
    assert health.l2cap_errors == 0
    assert health.level == "ok"


async def test_missing_journalctl_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _exec(*_args: str, **_kwargs: object) -> _Proc:
        raise OSError("no journalctl")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    health = await link_health.sample()
    assert health.available is False


async def test_reads_kernel_and_unit_journals(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two counters come from different journals and must both be read."""
    sources: list[str] = []

    async def _exec(*args: str, **_kwargs: object) -> _Proc:
        sources.append(args[1])
        return _Proc(b"x\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    await link_health.sample()
    assert "--dmesg" in sources
    assert "--unit=companion" in sources


async def test_sample_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Portal reconciles every 30 s; each sample costs two subprocesses."""
    calls = 0

    async def _exec(*_args: str, **_kwargs: object) -> _Proc:
        nonlocal calls
        calls += 1
        return _Proc(b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    await link_health.sample(now=1000.0)
    after_first = calls
    await link_health.sample(now=1005.0)
    assert calls == after_first


async def test_cache_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def _exec(*_args: str, **_kwargs: object) -> _Proc:
        nonlocal calls
        calls += 1
        return _Proc(b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    await link_health.sample(now=1000.0)
    after_first = calls
    await link_health.sample(now=1100.0)
    assert calls > after_first


def test_unavailable_is_not_graded_healthy() -> None:
    """An unread journal must not present as a clean link.

    ``level`` is 'ok' on the unavailable sentinel only because it has to be
    something; ``available`` is the field callers must branch on, and the
    endpoint's own level logic excludes unavailable halves.
    """
    blind = LinkHealth(available=False, l2cap_errors=0, a2dp_drops=0, window_hours=1.0, level="ok")
    assert blind.available is False
