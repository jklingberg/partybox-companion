"""Unit tests for the systemd-logind D-Bus power-off call (ADR-038).

Mocks dbus-fast's MessageBus/proxy chain rather than talking to a real
system bus — there is no logind to talk to in CI, and this module is a
single one-shot call with no protocol bytes to validate against a capture.

Known limitation: _FakeManagerIface is hand-rolled, so it can't catch a
wrong D-Bus method-name attribute the way a real ProxyInterface would —
dbus-fast generates call_<method> dynamically from introspected XML at
runtime, which nothing short of a real bus connection exercises. That gap
is exactly how a call_PowerOff/call_power_off casing bug shipped past this
suite and mypy both, and was only caught live on hardware (ADR-038).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from companion.services import login1_dbus


class _FakeManagerIface:
    def __init__(self) -> None:
        self.call_power_off = AsyncMock(return_value=None)


def _fake_bus(manager_iface: _FakeManagerIface) -> MagicMock:
    bus = MagicMock()
    bus.introspect = AsyncMock(return_value=MagicMock())
    proxy = MagicMock()
    proxy.get_interface.return_value = manager_iface
    bus.get_proxy_object.return_value = proxy
    bus.disconnect = MagicMock()
    return bus


async def test_power_off_calls_logind_poweroff_noninteractive() -> None:
    manager_iface = _FakeManagerIface()
    bus = _fake_bus(manager_iface)

    with patch.object(login1_dbus, "MessageBus") as message_bus_cls:
        message_bus_cls.return_value.connect = AsyncMock(return_value=bus)
        await login1_dbus.power_off()

    manager_iface.call_power_off.assert_awaited_once_with(False)
    bus.get_proxy_object.assert_called_once_with(
        login1_dbus._LOGIND_BUS_NAME,
        login1_dbus._LOGIND_PATH,
        bus.introspect.return_value,
    )


async def test_power_off_disconnects_even_if_call_fails() -> None:
    manager_iface = _FakeManagerIface()
    manager_iface.call_power_off = AsyncMock(side_effect=RuntimeError("boom"))
    bus = _fake_bus(manager_iface)

    with patch.object(login1_dbus, "MessageBus") as message_bus_cls:
        message_bus_cls.return_value.connect = AsyncMock(return_value=bus)
        with pytest.raises(RuntimeError):
            await login1_dbus.power_off()

    bus.disconnect.assert_called_once()
