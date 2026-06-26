# Examples

Executable documentation for the `partybox` SDK. Each script is small,
self-contained, and runnable against a real speaker — handy for manual testing,
debugging, and as living usage docs.

These require a Bluetooth adapter and a powered-on PartyBox in range. They use
only the public SDK API; no address configuration is needed — discovery finds
the speaker by its advertised name.

Run from the repository root:

```bash
uv run python examples/scan.py
uv run python examples/connect.py
uv run python examples/power_on.py
```

| Script | What it shows |
|---|---|
| [scan.py](scan.py) | Discover nearby PartyBox speakers |
| [connect.py](connect.py) | Find a speaker and open a control connection |
| [power_on.py](power_on.py) | Send a power-on command over the control transport |
