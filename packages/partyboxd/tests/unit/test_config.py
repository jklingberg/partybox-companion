"""Tests for partyboxd configuration."""

import pytest
from partyboxd.config import ServerSettings, Settings, SpeakerSettings


def test_defaults() -> None:
    s = Settings()
    assert s.speaker.scan_timeout == 8.0
    assert s.speaker.reconnect_delay == 5.0
    assert s.server.host == "127.0.0.1"
    assert s.server.port == 8765


def test_env_override_flat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PARTYBOXD_SERVER__PORT", "9000")
    s = Settings()
    assert s.server.port == 9000


def test_env_override_nested(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PARTYBOXD_SPEAKER__SCAN_TIMEOUT", "15.0")
    s = Settings()
    assert s.speaker.scan_timeout == 15.0


def test_speaker_settings_direct() -> None:
    s = SpeakerSettings(scan_timeout=12.0, reconnect_delay=3.0)
    assert s.scan_timeout == 12.0
    assert s.reconnect_delay == 3.0


def test_server_settings_direct() -> None:
    s = ServerSettings(host="192.168.1.1", port=80)
    assert s.host == "192.168.1.1"
    assert s.port == 80
