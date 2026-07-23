"""Unit tests for the systemd D-Bus StartUnit call (ADR-042).

Mocks dbus-fast's MessageBus/proxy chain rather than talking to a real
system bus — see test_login1_dbus.py's docstring for the same reasoning and
its one known limitation (a hand-rolled fake interface can't catch a wrong
D-Bus method-name attribute the way a real ProxyInterface would).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from companion.services import systemd1_dbus


class _FakeManagerIface:
    def __init__(self) -> None:
        self.call_start_unit = AsyncMock(return_value="/org/freedesktop/systemd1/job/1")


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
