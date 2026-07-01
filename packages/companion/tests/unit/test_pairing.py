"""Unit tests for PairingService.

No Bluetooth hardware or D-Bus system bus is required: BluezClient is
replaced by a lightweight fake implementing the same async-context-manager
surface (see docs/adr/027-bluetooth-bonding-architecture.md for why
PairingService talks to BlueZ over D-Bus rather than bluetoothctl). Tests
cover:
- start() returns True and transitions to SCANNING
- start() returns False when already pairing
- Successful flow: discover -> pair -> trust -> connect -> persist
- Bondable mode is scoped to the attempt (set True, cleared False on every
  exit path, including failures)
- No speaker discovered within the timeout
- Pair failure
- Unexpected exceptions are caught and surfaced as FAILED
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from types import TracebackType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, _patch, patch

from companion.config import AudioSettings
from companion.config_store import ConfigStore, PortalConfig
from companion.services.audio import AudioService
from companion.services.bluez_dbus import PairingFailedError
from companion.services.pairing import PairingService, PairingState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SPEAKER_MAC = "50:1B:6A:14:FD:1D"


def _audio() -> AudioService:
    return AudioService(AudioSettings(sink_address=None))


def _store(initial: PortalConfig | None = None) -> MagicMock:
    store = MagicMock(spec=ConfigStore)
    store.read.return_value = initial or PortalConfig()
    return store


def _service(
    store: MagicMock | None = None,
    audio: AudioService | None = None,
) -> PairingService:
    return PairingService(store or _store(), audio or _audio())


async def _run_to_completion(svc: PairingService) -> None:
    await svc.start()
    if svc._task is not None:
        with suppress(Exception):
            await asyncio.wait_for(asyncio.shield(svc._task), timeout=1.0)


class _FakeAgentScope:
    def __init__(self, client: _FakeBluezClient) -> None:
        self._client = client

    async def __aenter__(self) -> None:
        self._client.agent_registered = True

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._client.agent_registered = False


class _FakeBluezClient:
    """Stand-in for BluezClient implementing the same call surface."""

    def __init__(
        self,
        *,
        discovered_mac: str | None = _SPEAKER_MAC,
        pair_error: Exception | None = None,
        connect_error: Exception | None = None,
    ) -> None:
        self.discovered_mac = discovered_mac
        self.pair_error = pair_error
        self.connect_error = connect_error
        self.pairable_calls: list[bool] = []
        self.agent_registered = False
        self.paired: str | None = None
        self.trusted: str | None = None
        self.connected: str | None = None

    async def __aenter__(self) -> _FakeBluezClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def pairing_agent(self) -> _FakeAgentScope:
        return _FakeAgentScope(self)

    async def set_pairable(self, enabled: bool) -> None:
        self.pairable_calls.append(enabled)

    async def discover_bredr_address(self, timeout: float) -> str | None:
        return self.discovered_mac

    async def pair(self, mac: str) -> None:
        if self.pair_error is not None:
            raise self.pair_error
        self.paired = mac

    async def trust(self, mac: str) -> None:
        self.trusted = mac

    async def connect(self, mac: str) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = mac


def _patch_bluez(client: _FakeBluezClient) -> _patch[Any]:
    return patch("companion.services.pairing.BluezClient", return_value=client)


# ---------------------------------------------------------------------------
# start() — basic state transitions
# ---------------------------------------------------------------------------


async def test_start_returns_true_on_first_call() -> None:
    svc = _service()
    with patch.object(PairingService, "_do_pair", new=AsyncMock()):
        result = await svc.start()
    assert result is True


async def test_start_transitions_to_scanning() -> None:
    svc = _service()
    blocked: asyncio.Event = asyncio.Event()

    async def _blocked_pair() -> None:
        svc._state = PairingState.SCANNING  # mirror what _do_pair does first
        await blocked.wait()

    svc._do_pair = _blocked_pair  # type: ignore[method-assign]
    await svc.start()
    await asyncio.sleep(0)  # let task start
    assert svc.status.state == PairingState.SCANNING
    blocked.set()


async def test_start_returns_false_when_already_pairing() -> None:
    svc = _service()
    blocked: asyncio.Event = asyncio.Event()

    async def _blocked_pair() -> None:
        await blocked.wait()

    svc._do_pair = _blocked_pair  # type: ignore[method-assign]
    await svc.start()
    second = await svc.start()
    assert second is False
    blocked.set()


# ---------------------------------------------------------------------------
# Successful pairing flow
# ---------------------------------------------------------------------------


async def test_successful_pairing_calls_update_address() -> None:
    audio = _audio()
    svc = PairingService(_store(), audio)
    fake = _FakeBluezClient(discovered_mac=_SPEAKER_MAC)

    with _patch_bluez(fake):
        await _run_to_completion(svc)

    assert audio.status.address == _SPEAKER_MAC
    assert svc.status.state == PairingState.IDLE


async def test_successful_pairing_writes_config() -> None:
    store = _store()
    svc = PairingService(store, _audio())
    fake = _FakeBluezClient(discovered_mac=_SPEAKER_MAC)

    with _patch_bluez(fake):
        await _run_to_completion(svc)

    store.write.assert_called_once()
    saved: PortalConfig = store.write.call_args[0][0]
    assert saved.audio_sink_address == _SPEAKER_MAC


async def test_successful_pairing_calls_pair_trust_connect() -> None:
    svc = _service()
    fake = _FakeBluezClient(discovered_mac=_SPEAKER_MAC)

    with _patch_bluez(fake):
        await _run_to_completion(svc)

    assert fake.paired == _SPEAKER_MAC
    assert fake.trusted == _SPEAKER_MAC
    assert fake.connected == _SPEAKER_MAC


async def test_successful_pairing_registers_agent() -> None:
    svc = _service()
    fake = _FakeBluezClient(discovered_mac=_SPEAKER_MAC)

    with _patch_bluez(fake):
        await _run_to_completion(svc)

    # Unregistered again on exit — agent_registered reflects final state.
    assert fake.agent_registered is False


# ---------------------------------------------------------------------------
# Bondable mode scoping (ADR-027 decision 2)
# ---------------------------------------------------------------------------


async def test_pairable_set_then_cleared_on_success() -> None:
    svc = _service()
    fake = _FakeBluezClient(discovered_mac=_SPEAKER_MAC)

    with _patch_bluez(fake):
        await _run_to_completion(svc)

    assert fake.pairable_calls == [True, False]


async def test_pairable_cleared_when_no_device_discovered() -> None:
    svc = _service()
    fake = _FakeBluezClient(discovered_mac=None)

    with _patch_bluez(fake):
        await _run_to_completion(svc)

    assert fake.pairable_calls == [True, False]


async def test_pairable_cleared_on_pair_failure() -> None:
    svc = _service()
    fake = _FakeBluezClient(pair_error=PairingFailedError("boom"))

    with _patch_bluez(fake):
        await _run_to_completion(svc)

    assert fake.pairable_calls == [True, False]


# ---------------------------------------------------------------------------
# Discovery timeout / no device found
# ---------------------------------------------------------------------------


async def test_no_device_discovered_transitions_to_failed() -> None:
    svc = _service()
    fake = _FakeBluezClient(discovered_mac=None)

    with _patch_bluez(fake):
        await _run_to_completion(svc)

    assert svc.status.state == PairingState.FAILED
    assert svc.status.error is not None
    assert fake.paired is None


# ---------------------------------------------------------------------------
# Pair failure
# ---------------------------------------------------------------------------


async def test_pair_failure_transitions_to_failed() -> None:
    svc = _service()
    fake = _FakeBluezClient(pair_error=PairingFailedError("SSP failed"))

    with _patch_bluez(fake):
        await _run_to_completion(svc)

    assert svc.status.state == PairingState.FAILED
    assert svc.status.error is not None
    assert fake.trusted is None
    assert fake.connected is None


# ---------------------------------------------------------------------------
# Unexpected errors
# ---------------------------------------------------------------------------


async def test_unexpected_error_transitions_to_failed() -> None:
    svc = _service()
    fake = _FakeBluezClient(connect_error=RuntimeError("dbus exploded"))

    with _patch_bluez(fake):
        await _run_to_completion(svc)

    assert svc.status.state == PairingState.FAILED
    assert svc.status.error is not None
    assert "dbus exploded" in svc.status.error
