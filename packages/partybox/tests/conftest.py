"""Shared pytest fixtures for partybox tests."""

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "hardware: mark test as requiring a real PartyBox (skipped in CI)",
    )
