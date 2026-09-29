"""2.4 GHz RF congestion survey — the environmental cause of BT audio stutter.

Bluetooth and 2.4 GHz WiFi share one band. Bluetooth copes via Adaptive
Frequency Hopping (AFH): the controller classifies each of its 79 1 MHz
channels as usable or not and hops only across the usable ones. The spec
floor is 20 channels (Nmin) — a controller may not exclude more than that
however bad the interference gets, so a link in a crowded band ends up
hopping across a handful of channels that are themselves congested. The
audible result is intermittent stutter on a link whose every status signal
still reads healthy: connected, paired, streaming, no underruns.

Reading AFH directly would need an HCI ``Read AFH Channel Map`` command on a
raw socket (``CAP_NET_RAW``), which the hardened ``companion`` service user
deliberately does not have (ADR-019). What it *can* see, with the polkit
grants ADR-021 already installs for provisioning, is NetworkManager's WiFi
scan list — and neighbouring 2.4 GHz APs are the dominant interferer in
practice. This module turns that list into a coarse "how much of the band is
occupied" figure, which is a good enough proxy to tell the user their
environment is the problem. See ADR-044.

Deliberately *not* reused: ``ProvisioningService.scan_networks``. That path
de-duplicates by SSID because a user picks a network to join, not a radio —
but three APs sharing one SSID on one channel is exactly the situation this
module exists to detect, so collapsing them would hide the signal.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger(__name__)

#: nmcli SIGNAL (0-100) at or above which a neighbouring AP is treated as
#: strong enough to make a Bluetooth controller exclude its spectrum.
#: Calibrated against a hardware capture (2026-09-01, docs/validation): APs
#: at signal 69-85 had their spectrum excluded from the AFH map, while APs
#: at 60-64 on an otherwise-quiet channel did not. 65 sits in that gap. This
#: is a threshold on observed behaviour, not a figure from any spec.
_STRONG_SIGNAL = 65

#: Half-width of the spectrum one 20 MHz 802.11b/g/n channel occupies. The
#: channel is 20 MHz wide but the spectral mask spills either side, so 22 MHz
#: total is the figure normally used for adjacent-channel planning — which is
#: why 1/6/11 are the classic non-overlapping set.
_CHANNEL_HALF_WIDTH_MHZ = 11

#: Inclusive bounds of the 2.4 GHz ISM band, in MHz. Used both to filter a
#: dual-band scan down to 2.4 GHz and as the denominator for occupancy.
_BAND_START_MHZ = 2401
_BAND_END_MHZ = 2483

#: Fraction of the band that must be occupied by strong APs before the
#: environment is reported as congested. At 50% a Bluetooth link still has
#: roughly half the band to hop across; past that, AFH starts approaching
#: its floor and stutter becomes likely.
_CONGESTED_OCCUPANCY = 0.5

#: Occupancy at which the band goes from "fine" to "worth mentioning". One
#: 22 MHz WiFi channel is ~27% of the band and is unavoidable wherever 2.4
#: GHz WiFi exists at all, so the green threshold sits just above it: a
#: single channel is not a problem and must not be reported as one. Past
#: ~35% a second channel group is in play, and the hop space starts
#: shrinking meaningfully.
#:
#: Both figures are corroborated by the 2026-09-01 capture: 0.554 (channels
#: 1, 6 and 11 occupied) with the AFH map pinned at its 20-of-79 floor and
#: audio stuttering, versus 0.277 (channel 6 alone) with the map recovered
#: to 48 of 79 and the stutter gone.
#:
#: Note the scale is effectively quantised: real APs cluster on the three
#: non-overlapping channels, each ~27 points of occupancy, so a typical
#: environment steps 0.28 -> 0.55 -> 0.83 and skips the amber band entirely.
#: Amber is reached only by APs on overlapping, non-standard channels. That
#: is a property of how WiFi is deployed, not a gap in the thresholds.
_BAND_WARN_OCCUPANCY = 0.35


@dataclass(frozen=True)
class ChannelUsage:
    """How busy one 2.4 GHz WiFi channel is."""

    channel: int
    ap_count: int
    strongest_signal: int


Level = Literal["ok", "warn", "err"]


@dataclass(frozen=True)
class RfSurvey:
    """A coarse picture of 2.4 GHz congestion around the appliance.

    Carries no SSIDs or BSSIDs. Neighbouring SSIDs are effectively a
    location fingerprint (they are what public wardriving databases index
    on), and this is served from an unauthenticated endpoint alongside the
    other coarse status routes — so the identifying half of the scan is
    dropped here, at the point of collection, rather than filtered later.
    """

    ap_count: int
    strong_ap_count: int
    channels: list[ChannelUsage]
    occupied_mhz: int
    occupancy: float
    congested: bool
    level: Level


@dataclass(frozen=True)
class _Ap:
    """One access point from the scan, reduced to what congestion needs."""

    channel: int
    frequency_mhz: int
    signal: int


async def survey_24ghz(interface: str = "wlan0") -> RfSurvey | None:
    """Return a 2.4 GHz congestion survey, or ``None`` if unavailable.

    ``None`` means "could not look" — nmcli missing, the radio busy in AP
    mode during provisioning, or a scan list that came back empty — and is
    deliberately distinct from a survey reporting zero APs, which means "we
    looked and the band is clear". Callers surface the former as nothing at
    all rather than as a clean bill of health.
    """
    output = await _run_scan(interface)
    if output is None:
        return None
    aps = _parse_scan(output)
    if not aps:
        return None
    return _analyse(aps)


async def _run_scan(interface: str) -> str | None:
    """Read NetworkManager's cached scan list. Never triggers a fresh scan.

    ``--rescan no`` is the load-bearing flag. Forcing a scan makes the radio
    sweep every channel, which on a single shared antenna interrupts the very
    A2DP stream this survey exists to explain — a diagnostic that causes the
    fault it reports. NetworkManager refreshes this cache on its own schedule,
    so reading it costs nothing and the data is at most a few minutes stale,
    which is far finer than the timescale a neighbour's WiFi changes on.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "nmcli",
            "--mode",
            "multiline",
            "-t",
            "-f",
            "SIGNAL,FREQ,CHAN",
            "device",
            "wifi",
            "list",
            "ifname",
            interface,
            "--rescan",
            "no",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except OSError, TimeoutError:
        log.debug("RF survey: nmcli unavailable or timed out")
        return None
    if proc.returncode != 0:
        return None
    return (stdout or b"").decode(errors="replace")


def _parse_scan(output: str) -> list[_Ap]:
    """Parse ``nmcli --mode multiline`` output into 2.4 GHz APs.

    Each field arrives on its own ``KEY:VALUE`` line. A new record starts
    when SIGNAL appears while the current group already holds data — SIGNAL
    is requested first, so it is always the record's leading field. Rows are
    *not* de-duplicated: every BSS occupies spectrum, including several
    sharing one SSID, which is the case this module exists to catch.
    """
    groups: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                groups.append(current)
                current = {}
            continue
        key, _, value = line.partition(":")
        k = key.strip()
        if k == "SIGNAL" and current:
            groups.append(current)
            current = {}
        current[k] = value.strip()
    if current:
        groups.append(current)

    aps: list[_Ap] = []
    for g in groups:
        freq = _parse_int(g.get("FREQ", ""))
        chan = _parse_int(g.get("CHAN", ""))
        signal = _parse_int(g.get("SIGNAL", ""))
        if freq is None or chan is None or signal is None:
            continue
        if not _BAND_START_MHZ <= freq <= _BAND_END_MHZ:
            continue  # 5 GHz and 6 GHz radios do not contend with Bluetooth
        aps.append(_Ap(channel=chan, frequency_mhz=freq, signal=signal))
    return aps


def _parse_int(value: str) -> int | None:
    """Extract a leading integer, tolerating nmcli's unit suffixes.

    FREQ arrives as ``2412 MHz`` while SIGNAL and CHAN are bare integers, so
    everything is read through the same leading-digits rule rather than
    special-casing one field's formatting.
    """
    digits = ""
    for ch in value.strip():
        if not ch.isdigit():
            break
        digits += ch
    if not digits:
        return None
    return int(digits)


def _analyse(aps: list[_Ap]) -> RfSurvey:
    """Reduce scanned APs to per-channel usage and a band-occupancy figure.

    Occupancy is the union of the spectrum covered by *strong* APs, not a
    sum: ten APs stacked on one channel block the same 22 MHz that one AP
    does, and are correspondingly easier on Bluetooth than three APs spread
    across channels 1, 6 and 11 — which between them leave AFH almost
    nothing. Modelling it as a union is what makes the figure track what the
    controller actually experiences.
    """
    occupied: set[int] = set()
    for ap in aps:
        if ap.signal < _STRONG_SIGNAL:
            continue
        low = ap.frequency_mhz - _CHANNEL_HALF_WIDTH_MHZ
        high = ap.frequency_mhz + _CHANNEL_HALF_WIDTH_MHZ
        occupied.update(range(max(low, _BAND_START_MHZ), min(high, _BAND_END_MHZ) + 1))

    by_channel: dict[int, list[_Ap]] = {}
    for ap in aps:
        by_channel.setdefault(ap.channel, []).append(ap)
    channels = [
        ChannelUsage(
            channel=chan,
            ap_count=len(group),
            strongest_signal=max(a.signal for a in group),
        )
        for chan, group in sorted(by_channel.items())
    ]

    band_width = _BAND_END_MHZ - _BAND_START_MHZ + 1
    occupancy = len(occupied) / band_width
    level: Level = "ok"
    if occupancy >= _CONGESTED_OCCUPANCY:
        level = "err"
    elif occupancy >= _BAND_WARN_OCCUPANCY:
        level = "warn"
    return RfSurvey(
        ap_count=len(aps),
        strong_ap_count=sum(1 for a in aps if a.signal >= _STRONG_SIGNAL),
        channels=channels,
        occupied_mhz=len(occupied),
        occupancy=round(occupancy, 3),
        congested=occupancy >= _CONGESTED_OCCUPANCY,
        level=level,
    )
