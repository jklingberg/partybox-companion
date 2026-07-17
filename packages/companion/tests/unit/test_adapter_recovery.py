"""Tests for the adapter-recovery wrapper (ADR-039).

The real power-cycle needs BlueZ on a system D-Bus, which CI does not have —
these tests cover the wrapper's contract: it never raises, and every failure
shape collapses to False so the DeviceManager's retry loop is never harmed by
a broken recovery path.
"""

from __future__ import annotations

import pytest
from companion.services import adapter_recovery
from companion.services.adapter_recovery import reset_adapter


async def test_reset_adapter_returns_false_without_bluez() -> None:
    """In an environment with no system bus/BlueZ the helper subprocess
    prints an err: line; the wrapper must swallow it and report False."""
    assert await reset_adapter() is False


async def test_reset_adapter_times_out_to_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stuck helper subprocess is killed and reported as False."""
    import asyncio

    monkeypatch.setattr(adapter_recovery, "_RESET_TIMEOUT", 0.05)

    real_exec = asyncio.create_subprocess_exec

    async def sleepy_exec(*_args: object, **kwargs: object) -> asyncio.subprocess.Process:
        return await real_exec(
            adapter_recovery.sys.executable,
            "-c",
            "import time; time.sleep(60)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    monkeypatch.setattr(adapter_recovery.asyncio, "create_subprocess_exec", sleepy_exec)
    assert await reset_adapter() is False
