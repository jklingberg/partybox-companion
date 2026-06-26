"""Smoke test: the package imports cleanly."""

import partyboxd


def test_package_imports() -> None:
    assert partyboxd is not None
