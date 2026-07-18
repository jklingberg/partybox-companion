"""librespot --onevent hook: forwards PLAYER_EVENT to the running SpotifyService.

librespot runs this as a short-lived subprocess for every playback event
(play, pause, stop, ...), passing the event name via the PLAYER_EVENT
environment variable. SpotifyService generates the launcher script that
invokes this module (see ``_ensure_runtime_files``) and listens on a Unix
domain socket whose path is passed through COMPANION_SPOTIFY_EVENT_SOCK.

Best-effort by design: if SpotifyService isn't listening (e.g. mid-restart),
the event is silently dropped. `running` is unaffected, and the next event
resyncs playback state — no retry logic is worth the added complexity here.
"""

from __future__ import annotations

import os
import socket
import sys

_CONNECT_TIMEOUT = 1.0


def main() -> int:
    sock_path = os.environ.get("COMPANION_SPOTIFY_EVENT_SOCK")
    event = os.environ.get("PLAYER_EVENT", "")
    if not sock_path or not event:
        return 0
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(_CONNECT_TIMEOUT)
            sock.connect(sock_path)
            sock.sendall(event.encode() + b"\n")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
