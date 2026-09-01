"""Tests for the 2.4 GHz RF congestion survey and its endpoint.

The scan fixtures are real ``nmcli`` output shapes captured from the
appliance on 2026-09-01, before and after consolidating a home network's
2.4 GHz radios onto a single channel. On the "before" capture the speaker's
AFH map sat at 20 of 79 channels — the Bluetooth spec floor — and audio
stuttered; on the "after" capture it recovered to 48 of 79 and the stutter
stopped. Those two states are what ``congested`` has to separate, so they
are the fixtures rather than invented numbers.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest
from companion.config import SpotifySettings
from companion.config_store import ConfigStore
from companion.services import rf_survey
from companion.services.rf_survey import RfSurvey, _analyse, _parse_int, _parse_scan
from companion.services.router import make_services_router
from companion.services.spotify import SpotifyStatus
from httpx import ASGITransport, AsyncClient
from partyboxd.api import create_app as create_daemon_app
from partyboxd.api.auth import make_auth_dependency
from partyboxd.config import Settings as DaemonSettings
from partyboxd.device.manager import StatusSnapshot


def _scan(*rows: tuple[int, int, int]) -> str:
    """Render (signal, freq_mhz, channel) rows as nmcli multiline output."""
    out = []
    for signal, freq, chan in rows:
        out.append(f"SIGNAL:{signal}\nFREQ:{freq} MHz\nCHAN:{chan}")
    return "\n".join(out) + "\n"


# Nine APs across channels 1, 6 and 11 — the congested capture.
_BEFORE = _scan(
    (85, 2462, 11),
    (85, 2462, 11),
    (85, 2462, 11),
    (72, 2412, 1),
    (72, 2412, 1),
    (69, 2412, 1),
    (64, 2437, 6),
    (60, 2437, 6),
    (49, 2412, 1),
)

# The same home network after consolidating onto channel 6 alone.
_AFTER = _scan((92, 2437, 6), (59, 2437, 6), (59, 2437, 6), (50, 2437, 6))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_int_strips_nmcli_unit_suffix() -> None:
    assert _parse_int("2412 MHz") == 2412
    assert _parse_int("85") == 85
    assert _parse_int("") is None
    assert _parse_int("--") is None


def test_parse_scan_reads_every_ap() -> None:
    assert len(_parse_scan(_BEFORE)) == 9


def test_parse_scan_keeps_co_channel_aps_separate() -> None:
    """Several APs on one channel must not collapse into one another.

    ``ProvisioningService.scan_networks`` de-duplicates by SSID because a
    user joins a network, not a radio. Here the opposite is required: three
    APs sharing a channel are the signal, not noise.
    """
    aps = _parse_scan(_AFTER)
    assert len(aps) == 4
    assert {a.channel for a in aps} == {6}


def test_parse_scan_excludes_5ghz() -> None:
    """A 5 GHz radio does not contend with Bluetooth and must be ignored."""
    aps = _parse_scan(_scan((73, 5180, 36), (85, 2462, 11)))
    assert [a.frequency_mhz for a in aps] == [2462]


def test_parse_scan_skips_incomplete_records() -> None:
    assert _parse_scan("SIGNAL:85\nCHAN:11\n") == []


# ---------------------------------------------------------------------------
# Congestion analysis
# ---------------------------------------------------------------------------


def test_congested_band_is_flagged() -> None:
    survey = _analyse(_parse_scan(_BEFORE))
    assert survey.congested is True
    assert survey.ap_count == 9
    assert [c.channel for c in survey.channels] == [1, 6, 11]


def test_consolidated_band_is_not_flagged() -> None:
    survey = _analyse(_parse_scan(_AFTER))
    assert survey.congested is False
    assert survey.ap_count == 4


def test_occupancy_is_a_union_not_a_sum() -> None:
    """Stacking APs on one channel must not inflate occupancy.

    Ten APs on channel 6 block exactly the spectrum one AP on channel 6
    blocks. This is the property that makes the figure track what the
    Bluetooth controller actually experiences.
    """
    one = _analyse(_parse_scan(_scan((92, 2437, 6))))
    ten = _analyse(_parse_scan(_scan(*[(92, 2437, 6)] * 10)))
    assert one.occupied_mhz == ten.occupied_mhz
    assert ten.ap_count == 10


def test_spread_beats_stacking_for_congestion() -> None:
    """Three APs on 1/6/11 are worse than three stacked on one channel."""
    spread = _analyse(_parse_scan(_scan((85, 2412, 1), (85, 2437, 6), (85, 2462, 11))))
    stacked = _analyse(_parse_scan(_scan((85, 2437, 6), (85, 2437, 6), (85, 2437, 6))))
    assert spread.occupied_mhz > stacked.occupied_mhz
    assert spread.congested is True
    assert stacked.congested is False


def test_weak_aps_counted_but_do_not_occupy_spectrum() -> None:
    """A distant AP is still visible but does not force AFH exclusions."""
    survey = _analyse(_parse_scan(_scan((10, 2412, 1), (10, 2437, 6), (10, 2462, 11))))
    assert survey.ap_count == 3
    assert survey.strong_ap_count == 0
    assert survey.occupied_mhz == 0
    assert survey.congested is False


def test_empty_scan_is_an_empty_survey() -> None:
    survey = _analyse([])
    assert survey.ap_count == 0
    assert survey.congested is False


async def test_survey_returns_none_when_scan_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """No scan list is 'could not look', which must not read as 'band clear'."""

    async def _no_scan(_interface: str) -> str | None:
        return None

    monkeypatch.setattr(rf_survey, "_run_scan", _no_scan)
    assert await rf_survey.survey_24ghz() is None


async def test_survey_returns_none_on_empty_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _empty(_interface: str) -> str | None:
        return ""

    monkeypatch.setattr(rf_survey, "_run_scan", _empty)
    assert await rf_survey.survey_24ghz() is None


async def test_scan_never_triggers_a_rescan(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--rescan no`` is load-bearing: a scan would disturb the A2DP stream.

    Forcing NetworkManager to re-scan sweeps every channel on the shared
    radio, interrupting the audio this survey exists to explain — a
    diagnostic that causes the fault it reports.
    """
    captured: list[str] = []

    class _Proc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def _fake_exec(*args: str, **_kwargs: object) -> _Proc:
        captured.extend(args)
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    await rf_survey._run_scan("wlan0")
    assert "--rescan" in captured
    assert captured[captured.index("--rescan") + 1] == "no"


def test_survey_carries_no_ssids() -> None:
    """The response must not be a location fingerprint (ADR-037, ADR-044)."""
    survey = _analyse(_parse_scan(_BEFORE))
    assert not any("ssid" in f.lower() for f in survey.__dataclass_fields__)


# ---------------------------------------------------------------------------
# GET /api/v1/rf
# ---------------------------------------------------------------------------


def _make_client(survey: RfSurvey | None, *, with_auth: bool = False) -> AsyncClient:
    import tempfile

    spotify = MagicMock()
    type(spotify).status = PropertyMock(
        return_value=SpotifyStatus(running=True, state="stopped", device_name="PartyBox")
    )
    spotify.settings = SpotifySettings(connect_name="PartyBox", bitrate=320)

    manager = MagicMock()
    type(manager).snapshot = PropertyMock(
        return_value=StatusSnapshot(connected=False, address=None, firmware=None, battery=None)
    )
    manager.subscribe = MagicMock(return_value=asyncio.Queue())
    manager.unsubscribe = MagicMock()

    settings = DaemonSettings()
    app = create_daemon_app(manager, settings)
    app.include_router(
        make_services_router(
            spotify,
            ConfigStore(Path(tempfile.mkdtemp()) / "config.json"),
            auth=make_auth_dependency(settings) if with_auth else None,
            rf_survey_fn=AsyncMock(return_value=survey),
        )
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_rf_endpoint_reports_congestion() -> None:
    async with _make_client(_analyse(_parse_scan(_BEFORE))) as client:
        r = await client.get("/api/v1/rf")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["congested"] is True
    assert body["ap_count"] == 9
    assert [c["channel"] for c in body["channels"]] == [1, 6, 11]


async def test_rf_endpoint_reports_quiet_band() -> None:
    async with _make_client(_analyse(_parse_scan(_AFTER))) as client:
        r = await client.get("/api/v1/rf")
    body = r.json()
    assert body["available"] is True
    assert body["congested"] is False


async def test_rf_endpoint_unavailable_is_not_an_all_clear() -> None:
    async with _make_client(None) as client:
        r = await client.get("/api/v1/rf")
    assert r.status_code == 200
    assert r.json()["available"] is False


async def test_rf_endpoint_is_unauthenticated() -> None:
    """Coarse counts with no SSIDs — readable by the Portal with no API key."""
    async with _make_client(_analyse(_parse_scan(_AFTER)), with_auth=True) as client:
        r = await client.get("/api/v1/rf")
    assert r.status_code == 200
