"""Classic Bluetooth (A2DP) device control via ``bluetoothctl``.

This drives the *audio* side of the speaker — the BR/EDR address that carries
A2DP — using the standard ``bluetoothctl`` CLI. The BLE *control* side is the
SDK's job and lives in :mod:`m3lib.control`.

Why ``bluetoothctl`` and not BlueZ D-Bus: the spike values reproducibility over
elegance. Anything a script does here, a human can do by hand to confirm the
finding. The functions are intentionally small wrappers around single
subcommands; parsing is limited to ``bluetoothctl info``, whose ``Key: value``
output is stable across BlueZ 5.x.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from . import proc

#: Substring shared by every PartyBox advertised name (520, 310, ...).
PARTYBOX_NAME = "PartyBox"

_DEVICE_LINE = re.compile(r"^Device ([0-9A-F:]{17}) (.+)$", re.MULTILINE)
_INFO_BOOL = {"yes": True, "no": False}


@dataclass(frozen=True)
class DeviceInfo:
    """Parsed ``bluetoothctl info`` for one device."""

    address: str
    name: str | None
    paired: bool
    bonded: bool
    trusted: bool
    connected: bool
    uuids: tuple[str, ...]

    @property
    def has_a2dp_sink(self) -> bool:
        # 0000110b = Audio Sink: the speaker accepts an A2DP stream from us.
        return any("0000110b" in u.lower() for u in self.uuids)


async def info(mac: str) -> DeviceInfo | None:
    """Return parsed info for ``mac``, or ``None`` if BlueZ doesn't know it."""
    result = await proc.run("bluetoothctl", "info", mac, timeout=15.0)
    if not result.ok or "Device " not in result.stdout:
        return None

    fields: dict[str, str] = {}
    uuids: list[str] = []
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if line.startswith("UUID:"):
            match = re.search(r"\(([0-9a-fA-F-]+)\)", line)
            if match:
                uuids.append(match.group(1))
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()

    return DeviceInfo(
        address=mac,
        name=fields.get("Name") or fields.get("Alias"),
        paired=_INFO_BOOL.get(fields.get("Paired", "no"), False),
        bonded=_INFO_BOOL.get(fields.get("Bonded", "no"), False),
        trusted=_INFO_BOOL.get(fields.get("Trusted", "no"), False),
        connected=_INFO_BOOL.get(fields.get("Connected", "no"), False),
        uuids=tuple(uuids),
    )


async def discover(name: str = PARTYBOX_NAME, *, timeout: float = 12.0) -> list[tuple[str, str]]:
    """Scan and return ``(address, name)`` for devices whose name matches.

    Runs a timed ``scan on`` then lists known devices. Discovers both BR/EDR and
    LE; for A2DP you want the BR/EDR address, which on the PartyBox is distinct
    from its LE control identity.
    """
    await proc.run(
        "bluetoothctl", "--timeout", str(int(timeout)), "scan", "on", timeout=timeout + 5
    )
    listing = await proc.run("bluetoothctl", "devices", timeout=15.0)
    found: list[tuple[str, str]] = []
    for addr, dev_name in _DEVICE_LINE.findall(listing.stdout):
        if name.lower() in dev_name.lower():
            found.append((addr, dev_name))
    return found


async def pair(mac: str) -> proc.Result:
    """Attempt to pair+bond with ``mac`` (speaker must be awake / discoverable).

    Best-effort: the PartyBox uses Just-Works pairing, so the default BlueZ
    agent usually suffices. If this fails, bond once by hand (see the README)
    and re-run — bonding state then persists.
    """
    return await proc.run("bluetoothctl", "pair", mac, timeout=30.0)


async def trust(mac: str) -> proc.Result:
    return await proc.run("bluetoothctl", "trust", mac, timeout=15.0)


async def connect(mac: str) -> proc.Result:
    return await proc.run("bluetoothctl", "connect", mac, timeout=30.0)


async def disconnect(mac: str) -> proc.Result:
    return await proc.run("bluetoothctl", "disconnect", mac, timeout=20.0)


async def wait_connected(mac: str, *, timeout: float = 30.0, want: bool = True) -> bool:
    """Poll until ``connected`` reaches ``want`` or ``timeout`` elapses."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        current = await info(mac)
        if current is not None and current.connected == want:
            return True
        await asyncio.sleep(1.0)
    return False
