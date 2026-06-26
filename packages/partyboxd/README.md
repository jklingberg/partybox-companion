# partyboxd

Headless daemon exposing a stable HTTP API for PartyBox speakers.

Wraps the [`partybox`](../partybox) Bluetooth SDK in a FastAPI service so other
processes can control a speaker over HTTP. Run it with the `partyboxd` entry point.

Part of the [partybox-companion](https://github.com/jklingberg/partybox-companion) workspace.
