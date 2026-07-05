"""Unit tests for ConfigStore, including corrupt-file resilience.

A damaged config.json (truncated write, SD corruption, manual editing) must
never prevent the appliance from starting — see FAULT-04 in
docs/validation/appliance-validation.md.
"""

from __future__ import annotations

from pathlib import Path

from companion.config_store import ConfigStore, PortalConfig


def test_missing_file_returns_defaults(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    assert store.read() == PortalConfig()


def test_roundtrip(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.write(PortalConfig(device_name="Den", spotify_bitrate=160))
    cfg = store.read()
    assert cfg.device_name == "Den"
    assert cfg.spotify_bitrate == 160


def test_corrupt_json_falls_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{broken json!!")
    store = ConfigStore(path)
    cfg = store.read()
    assert cfg == PortalConfig()
    # Original quarantined for diagnosis, not silently deleted.
    assert not path.exists()
    quarantined = list(tmp_path.glob("config.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text() == "{broken json!!"


def test_invalid_schema_falls_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"spotify_bitrate": 12345}')
    store = ConfigStore(path)
    assert store.read() == PortalConfig()


def test_write_after_quarantine_starts_clean(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{broken json!!")
    store = ConfigStore(path)
    store.read()
    store.write(PortalConfig(device_name="Fixed"))
    assert store.read().device_name == "Fixed"


def test_reset_deletes_file_and_restores_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    store.write(PortalConfig(device_name="Den", audio_sink_address="50:1B:6A:14:FD:1D"))
    assert path.exists()

    store.reset()

    assert not path.exists()
    assert store.read() == PortalConfig()


def test_reset_is_noop_when_file_missing(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.reset()  # must not raise
    assert store.read() == PortalConfig()
