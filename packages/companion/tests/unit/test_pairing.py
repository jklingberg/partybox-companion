"""Unit tests for PairingService.

No Bluetooth hardware or bluetoothctl binary is required. Tests cover:
- start() returns True and transitions to SCANNING
- start() returns False when already pairing
- Successful flow: scan → find JBL → pair → trust → connect → persist
- Scan timeout when no JBL device appears
- Pair command failure
- Config store is written with discovered address on success
- AudioService.update_address() is called on success
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock, MagicMock, patch

from companion.config import AudioSettings
from companion.config_store import ConfigStore, PortalConfig
from companion.services.audio import AudioService
from companion.services.pairing import PairingService, PairingState, _list_devices

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SPEAKER_MAC = "50:1B:6A:14:FD:1D"
_SPEAKER_NAME = "JBL PartyBox 520"


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


def _mock_proc(stdout: bytes = b"", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    proc.wait = AsyncMock(return_value=returncode)
    proc.terminate = MagicMock()
    return proc


# ---------------------------------------------------------------------------
# start() — basic state transitions
# ---------------------------------------------------------------------------


async def test_start_returns_true_on_first_call() -> None:
    svc = _service()
    with patch("companion.services.pairing.PairingService._do_pair", new=AsyncMock()):
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


async def test_pre_discovered_device_skips_scan() -> None:
    """If the speaker's BR/EDR address is already in BlueZ cache, pair immediately."""
    audio = _audio()
    store = _store()
    svc = PairingService(store, audio)

    scan_started = False

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        nonlocal scan_started
        scan_started = True
        return _mock_proc()

    with (
        patch(
            "companion.services.pairing._list_devices",
            new=AsyncMock(return_value={_SPEAKER_MAC: _SPEAKER_NAME}),
        ),
        patch("companion.services.pairing._is_public_address", new=AsyncMock(return_value=True)),
        patch("companion.services.pairing.asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch("companion.services.pairing._btctl", new=AsyncMock()),
        patch("companion.services.pairing.PairingService._pair", new=AsyncMock(return_value=True)),
    ):
        await svc.start()
        if svc._task:
            with suppress(Exception):
                await asyncio.wait_for(asyncio.shield(svc._task), timeout=1.0)

    assert audio.status.address == _SPEAKER_MAC
    assert not scan_started  # bluetoothctl scan on was never launched


async def test_successful_pairing_calls_update_address() -> None:
    audio = _audio()
    store = _store()
    svc = PairingService(store, audio)

    devices_before: dict[str, str] = {}
    devices_after: dict[str, str] = {_SPEAKER_MAC: _SPEAKER_NAME}

    call_count = 0

    async def fake_list_devices() -> dict[str, str]:
        nonlocal call_count
        call_count += 1
        return devices_before if call_count == 1 else devices_after

    scan_proc = _mock_proc()

    with (
        patch("companion.services.pairing._list_devices", side_effect=fake_list_devices),
        patch("companion.services.pairing._is_public_address", new=AsyncMock(return_value=True)),
        patch(
            "companion.services.pairing.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=scan_proc),
        ),
        patch("companion.services.pairing._btctl", new=AsyncMock()),
        patch("companion.services.pairing.asyncio.sleep", new=AsyncMock()),
        patch("companion.services.pairing.PairingService._pair", new=AsyncMock(return_value=True)),
    ):
        await svc.start()
        await asyncio.sleep(0)
        # Let the background task run to completion
        if svc._task:
            with suppress(Exception):
                await asyncio.wait_for(asyncio.shield(svc._task), timeout=1.0)

    assert audio.status.address == _SPEAKER_MAC


async def test_successful_pairing_writes_config() -> None:
    audio = _audio()
    store = _store()
    svc = PairingService(store, audio)

    async def fake_list_devices_seq() -> dict[str, str]:
        return {_SPEAKER_MAC: _SPEAKER_NAME}

    with (
        patch(
            "companion.services.pairing._list_devices",
            new=AsyncMock(
                side_effect=[
                    {},  # pre-scan snapshot
                    {_SPEAKER_MAC: _SPEAKER_NAME},  # first poll
                ]
            ),
        ),
        patch("companion.services.pairing._is_public_address", new=AsyncMock(return_value=True)),
        patch(
            "companion.services.pairing.asyncio.create_subprocess_exec",
            return_value=_mock_proc(),
        ),
        patch("companion.services.pairing._btctl", new=AsyncMock()),
        patch("companion.services.pairing.asyncio.sleep", new=AsyncMock()),
        patch("companion.services.pairing.PairingService._pair", new=AsyncMock(return_value=True)),
    ):
        await svc.start()
        if svc._task:
            with suppress(Exception):
                await asyncio.wait_for(asyncio.shield(svc._task), timeout=1.0)

    store.write.assert_called_once()
    saved: PortalConfig = store.write.call_args[0][0]
    assert saved.audio_sink_address == _SPEAKER_MAC


# ---------------------------------------------------------------------------
# Scan timeout
# ---------------------------------------------------------------------------


async def test_scan_timeout_transitions_to_failed() -> None:
    svc = _service()

    with (
        patch("companion.services.pairing._list_devices", new=AsyncMock(return_value={})),
        patch(
            "companion.services.pairing.asyncio.create_subprocess_exec",
            return_value=_mock_proc(),
        ),
        patch("companion.services.pairing._SCAN_TIMEOUT", 0.0),
        patch("companion.services.pairing.asyncio.sleep", new=AsyncMock()),
    ):
        await svc.start()
        if svc._task:
            with suppress(Exception):
                await asyncio.wait_for(asyncio.shield(svc._task), timeout=1.0)

    assert svc.status.state == PairingState.FAILED
    assert svc.status.error is not None


# ---------------------------------------------------------------------------
# _pair output parsing
# ---------------------------------------------------------------------------


async def test_pair_returns_true_on_success_output() -> None:
    svc = _service()
    output = b"Attempting to pair...\n[CHG] Device ... Paired: yes\nPairing successful\n"
    with patch(
        "companion.services.pairing.asyncio.create_subprocess_exec",
        return_value=_mock_proc(output),
    ):
        assert await svc._pair(_SPEAKER_MAC) is True


async def test_pair_returns_false_on_auth_failed() -> None:
    svc = _service()
    output = (
        b"Attempting to pair...\n"
        b"[CHG] Device ... Connected: yes\n"
        b"Failed to pair: org.bluez.Error.AuthenticationFailed\n"
    )
    with patch(
        "companion.services.pairing.asyncio.create_subprocess_exec",
        return_value=_mock_proc(output),
    ):
        assert await svc._pair(_SPEAKER_MAC) is False


async def test_pair_returns_false_on_empty_output() -> None:
    """Empty output must not be treated as success."""
    svc = _service()
    with patch(
        "companion.services.pairing.asyncio.create_subprocess_exec",
        return_value=_mock_proc(b""),
    ):
        assert await svc._pair(_SPEAKER_MAC) is False


async def test_pair_returns_true_on_already_paired() -> None:
    svc = _service()
    output = b"Failed to pair: org.bluez.Error.AlreadyExists\n"
    with patch(
        "companion.services.pairing.asyncio.create_subprocess_exec",
        return_value=_mock_proc(output),
    ):
        assert await svc._pair(_SPEAKER_MAC) is True


# ---------------------------------------------------------------------------
# _list_devices parser
# ---------------------------------------------------------------------------


async def test_list_devices_parses_output() -> None:
    output = (
        b"Device 50:1B:6A:14:FD:1D JBL PartyBox 520\n"
        b"Device 70:8B:BE:97:94:ED JBL PartyBox 520\n"
        b"Device AA:BB:CC:DD:EE:FF Some Other Device\n"
    )
    with patch(
        "companion.services.pairing.asyncio.create_subprocess_exec",
        return_value=_mock_proc(output),
    ):
        result = await _list_devices()

    assert result == {
        "50:1B:6A:14:FD:1D": "JBL PartyBox 520",
        "70:8B:BE:97:94:ED": "JBL PartyBox 520",
        "AA:BB:CC:DD:EE:FF": "Some Other Device",
    }


async def test_list_devices_handles_oserror() -> None:
    with patch(
        "companion.services.pairing.asyncio.create_subprocess_exec",
        side_effect=OSError("bluetoothctl not found"),
    ):
        result = await _list_devices()
    assert result == {}
