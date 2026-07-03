#!/usr/bin/env python3
"""Query the partyboxd health and speaker endpoints.

Requires a running partyboxd instance (start with ``partyboxd`` or
``python examples/start_daemon.py``).

uv run python examples/query_status.py
uv run python examples/query_status.py --host 192.168.1.10 --port 8765
uv run python examples/query_status.py --api-key mysecretkey
"""

import argparse
import json
import urllib.request
from typing import Any


def _get(base: str, path: str, api_key: str | None) -> dict[str, Any]:
    req = urllib.request.Request(f"{base}{path}")  # noqa: S310
    if api_key:
        req.add_header("X-Api-Key", api_key)
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
        data: dict[str, Any] = json.loads(resp.read())
        return data


def main() -> None:
    parser = argparse.ArgumentParser(description="Query partyboxd status")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--api-key", default=None, help="X-Api-Key header (if configured)")
    args = parser.parse_args()

    base = f"http://{args.host}:{args.port}"
    try:
        health = _get(base, "/api/v1/health", args.api_key)
        speaker = _get(base, "/api/v1/speaker", args.api_key)
    except OSError as exc:
        print(f"Could not reach daemon at {base}: {exc}")
        raise SystemExit(1) from exc

    print(f"status        : {health['status']} (v{health['version']})")
    print(f"ble connected : {health['ble_connected']}")
    audio_ready = health["audio_ready"]
    print(f"audio ready   : {audio_ready if audio_ready is not None else '— (standalone daemon)'}")
    print(f"address       : {speaker['address'] or '—'}")
    print(f"firmware      : {speaker['firmware'] or '—'}")
    battery = speaker["battery"]
    print(f"battery       : {battery}%" if battery is not None else "battery       : —")


if __name__ == "__main__":
    main()
