"""Structural guard for the HostOriginMiddleware / CaptivePortalMiddleware order.

``test_host_origin_provisioning_interaction.py`` proves the *behavior* that
depends on this ordering (captive-portal probes still redirect during AP
mode, everything else still gets Host/Origin validation) -- but it builds its
own app by hand and never touches ``companion/__main__.py`` itself. A future
edit to ``_run()`` that reorders the two ``add_middleware`` calls (directly,
or indirectly by refactoring router/middleware setup into a helper) would
silently invert the effective execution order without that test -- or any
other behavioral test -- ever running against the real wiring to catch it.

This test closes that gap the cheap way: it asserts, against the actual
source of ``companion/__main__.py``, that ``CaptivePortalMiddleware`` is
still registered via ``add_middleware()`` *after* ``create_daemon_app()``
has run (which is what registers ``HostOriginMiddleware`` -- see
``partyboxd.api.app.create_app``). Starlette's ``add_middleware()`` makes the
most-recently-added middleware outermost, so this source order is exactly
what makes CaptivePortalMiddleware intercept probe paths before
HostOriginMiddleware ever sees them. See docs/adr/041-host-origin-allowlist.md.

If this test fails, it means someone reordered the two calls -- captive
portal detection during AP-mode provisioning (ADR-021) will silently break,
even though every unit test for HostOriginMiddleware in isolation keeps
passing.
"""

from __future__ import annotations

import inspect

import companion.__main__ as companion_main


def test_captive_portal_middleware_added_after_daemon_app_creation() -> None:
    source = inspect.getsource(companion_main._run)

    create_app_pos = source.find("create_daemon_app(")
    add_captive_portal_pos = source.find("add_middleware(CaptivePortalMiddleware")

    assert create_app_pos != -1, "create_daemon_app(...) call not found in _run()"
    assert add_captive_portal_pos != -1, (
        "app.add_middleware(CaptivePortalMiddleware, ...) call not found in _run()"
    )
    assert create_app_pos < add_captive_portal_pos, (
        "CaptivePortalMiddleware must be registered via add_middleware() AFTER "
        "create_daemon_app() -- Starlette's add_middleware() makes the most "
        "recently added middleware outermost, so this order is what lets "
        "CaptivePortalMiddleware intercept AP-mode captive-portal probes "
        "before HostOriginMiddleware (added inside create_daemon_app()) "
        "rejects their foreign Host header. See "
        "docs/adr/041-host-origin-allowlist.md."
    )
