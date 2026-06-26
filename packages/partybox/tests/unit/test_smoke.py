"""Smoke test: the package imports cleanly."""

import partybox


def test_package_imports() -> None:
    assert partybox is not None
