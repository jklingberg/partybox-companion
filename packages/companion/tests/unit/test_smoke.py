"""Smoke test: the package imports cleanly."""

import companion


def test_package_imports() -> None:
    assert companion is not None
