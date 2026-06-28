"""partybox CLI — Post-v1.0.

A dedicated CLI is deferred after v1.0. The Companion Portal at
http://<appliance>:8080 is the primary interface; the REST API at
/api/v1/ is the primary integration surface.
"""

from __future__ import annotations

import typer

app = typer.Typer(
    name="partybox",
    help=(
        "partybox CLI\n\n"
        "The CLI is coming in a future release.\n\n"
        "In the meantime:\n\n"
        "  • Companion Portal:  http://<appliance>:8080\n"
        "  • REST API:          http://<appliance>:8080/api/v1/\n"
        "  • Interactive docs:  http://<appliance>:8080/api/docs\n"
    ),
    no_args_is_help=True,
)
