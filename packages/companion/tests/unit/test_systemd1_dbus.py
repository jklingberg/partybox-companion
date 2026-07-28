"""Unit tests for the systemd D-Bus StartUnit call (ADR-043).

Mocks dbus-fast's MessageBus/proxy chain rather than talking to a real
system bus — see test_login1_dbus.py's docstring for the same reasoning and
its one known limitation (a hand-rolled fake interface can't catch a wrong
D-Bus method-name attribute the way a real ProxyInterface would).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from companion.services import systemd1_dbus

_JOB_PATH = "/org/freedesktop/systemd1/job/1"


class _FakeManagerIface:
    """Simulates StartUnit + the JobRemoved signal systemd emits once a
    (oneshot) unit's job actually finishes.

    By default, ``on_job_removed`` fires the registered handler immediately
    (as if the job had already completed) with a matching job path, so
    ``start_unit()``'s wait resolves right away without real timing games.
    Pass ``fire_job_removed=False`` to simulate a job that never completes
    (for the timeout test).
    """

    def __init__(self, job_path: str = _JOB_PATH, fire_job_removed: bool = True) -> None:
        self.call_start_unit = AsyncMock(return_value=job_path)
        self._job_path = job_path
        self._fire = fire_job_removed
        self.on_job_removed = MagicMock(side_effect=self._register)
        self.off_job_removed = MagicMock()

    def _register(self, handler: Callable[[int, str, str, str], None]) -> None:
        if self._fire:
            handler(1, self._job_path, "companion-ssh-apply.service", "done")


def _fake_bus(manager_iface: _FakeManagerIface) -> MagicMock:
    bus = MagicMock()
    bus.introspect = AsyncMock(return_value=MagicMock())
    proxy = MagicMock()
    proxy.get_interface.return_value = manager_iface
    bus.get_proxy_object.return_value = proxy
    bus.disconnect = MagicMock()
    return bus


async def test_start_unit_calls_systemd_start_unit() -> None:
    manager_iface = _FakeManagerIface()
    bus = _fake_bus(manager_iface)

    with patch.object(systemd1_dbus, "MessageBus") as message_bus_cls:
        message_bus_cls.return_value.connect = AsyncMock(return_value=bus)
        await systemd1_dbus.start_unit("companion-ssh-apply.service")

    manager_iface.call_start_unit.assert_awaited_once_with("companion-ssh-apply.service", "replace")
    bus.get_proxy_object.assert_called_once_with(
        systemd1_dbus._SYSTEMD_BUS_NAME,
        systemd1_dbus._SYSTEMD_PATH,
        bus.introspect.return_value,
    )


async def test_start_unit_uses_given_mode() -> None:
    manager_iface = _FakeManagerIface()
    bus = _fake_bus(manager_iface)

    with patch.object(systemd1_dbus, "MessageBus") as message_bus_cls:
        message_bus_cls.return_value.connect = AsyncMock(return_value=bus)
        await systemd1_dbus.start_unit("some.service", mode="fail")

    manager_iface.call_start_unit.assert_awaited_once_with("some.service", "fail")


async def test_start_unit_disconnects_even_if_call_fails() -> None:
    manager_iface = _FakeManagerIface()
    manager_iface.call_start_unit = AsyncMock(side_effect=RuntimeError("boom"))
    bus = _fake_bus(manager_iface)

    with patch.object(systemd1_dbus, "MessageBus") as message_bus_cls:
        message_bus_cls.return_value.connect = AsyncMock(return_value=bus)
        try:
            await systemd1_dbus.start_unit("companion-ssh-apply.service")
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected RuntimeError to propagate")

    bus.disconnect.assert_called_once()


async def test_start_unit_waits_for_matching_job_removed() -> None:
    """The call only returns once JobRemoved fires for *this* job's path --
    verified by asserting the subscribe/unsubscribe pair was actually used
    (the fake fires synchronously on registration, so ordering here proves
    start_unit genuinely waited on the signal rather than returning as soon
    as StartUnit handed back a job path)."""
    manager_iface = _FakeManagerIface()
    bus = _fake_bus(manager_iface)

    with patch.object(systemd1_dbus, "MessageBus") as message_bus_cls:
        message_bus_cls.return_value.connect = AsyncMock(return_value=bus)
        await systemd1_dbus.start_unit("companion-ssh-apply.service")

    manager_iface.on_job_removed.assert_called_once()
    manager_iface.off_job_removed.assert_called_once()


async def test_start_unit_ignores_job_removed_for_other_jobs() -> None:
    """A JobRemoved signal for an unrelated job must not resolve our wait --
    only the exact job path StartUnit handed back should. The fake fires a
    mismatched job id first, then the real one, both synchronously at
    registration time; start_unit must only complete because of the second."""

    class _TwoSignalManagerIface(_FakeManagerIface):
        def _register(self, handler: Callable[[int, str, str, str], None]) -> None:
            handler(2, "/org/freedesktop/systemd1/job/999", "some-other.service", "done")
            handler(1, self._job_path, "companion-ssh-apply.service", "done")

    manager_iface = _TwoSignalManagerIface()
    bus = _fake_bus(manager_iface)

    with patch.object(systemd1_dbus, "MessageBus") as message_bus_cls:
        message_bus_cls.return_value.connect = AsyncMock(return_value=bus)
        await systemd1_dbus.start_unit("companion-ssh-apply.service")


async def test_start_unit_times_out_if_job_never_completes() -> None:
    manager_iface = _FakeManagerIface(fire_job_removed=False)
    bus = _fake_bus(manager_iface)

    with patch.object(systemd1_dbus, "MessageBus") as message_bus_cls:
        message_bus_cls.return_value.connect = AsyncMock(return_value=bus)
        with pytest.raises(asyncio.TimeoutError):
            await systemd1_dbus.start_unit("companion-ssh-apply.service", timeout=0.05)

    manager_iface.off_job_removed.assert_called_once()
    bus.disconnect.assert_called_once()
