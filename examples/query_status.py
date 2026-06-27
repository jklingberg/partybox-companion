#!/usr/bin/env python3
"""Query the partyboxd status endpoint.

Requires a running partyboxd instance (start with ``partyboxd`` or
``python examples/start_daemon.py``).

uv run python examples/query_status.py
uv run python examples/query_status.py --host 192.168.1.10 --port 8765
"""

import argparse
import json
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser(description="Query partyboxd status")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}/api/v1/status"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
            data = json.loads(resp.read())
    except OSError as exc:
        print(f"Could not reach daemon at {url}: {exc}")
        raise SystemExit(1) from exc

    print(f"connected : {data['connected']}")
    print(f"healthy   : {data['healthy']}")
    print(f"address   : {data['address'] or '—'}")
    print(f"firmware  : {data['firmware'] or '—'}")
    battery = data["battery"]
    print(f"battery   : {battery}%" if battery is not None else "battery   : —")


if __name__ == "__main__":
    main()
