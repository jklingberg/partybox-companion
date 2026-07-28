"""Unit tests for ConfigStore, including corrupt-file resilience.

A damaged config.json (truncated write, SD corruption, manual editing) must
never prevent the appliance from starting — see FAULT-04 in
docs/validation/appliance-validation.md.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

from companion.config_store import ConfigStore, PortalConfig


def test_missing_file_returns_defaults(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    assert store.read() == PortalConfig()


def test_roundtrip(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.write(PortalConfig(spotify_connect_name="Den", spotify_bitrate=160))
    cfg = store.read()
    assert cfg.spotify_connect_name == "Den"
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
    store.write(PortalConfig(spotify_connect_name="Fixed"))
    assert store.read().spotify_connect_name == "Fixed"


def test_reset_deletes_file_and_restores_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    store.write(PortalConfig(spotify_connect_name="Den", audio_sink_address="50:1B:6A:14:FD:1D"))
    assert path.exists()

    store.reset()

    assert not path.exists()
    assert store.read() == PortalConfig()


def test_reset_is_noop_when_file_missing(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.reset()  # must not raise
    assert store.read() == PortalConfig()


# ---------------------------------------------------------------------------
# Atomic write (DEBT-03)
# ---------------------------------------------------------------------------


def test_write_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.write(PortalConfig(spotify_connect_name="Den"))
    assert list(tmp_path.glob("*.tmp-*")) == []


def test_write_does_not_touch_original_until_replace(tmp_path: Path) -> None:
    """A crash between the temp-file write and the rename must leave the
    previous, still-valid config.json completely untouched — never a
    truncated or half-written file (the failure mode a plain `write_text`
    is vulnerable to on power loss).
    """
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    store.write(PortalConfig(spotify_connect_name="Original"))

    with (
        patch("companion.config_store.os.replace", side_effect=OSError("simulated power loss")),
        suppress(OSError),
    ):
        store.write(PortalConfig(spotify_connect_name="New"))

    # Original file is exactly as it was — no truncation, no partial write.
    assert PortalConfig.model_validate_json(path.read_text()).spotify_connect_name == "Original"
    # The temp file used for the attempted write is cleaned up, not left
    # behind as a second, inconsistent copy of the config.
    assert list(tmp_path.glob("*.tmp-*")) == []


# ---------------------------------------------------------------------------
# Atomic read-modify-write (RACE-01)
# ---------------------------------------------------------------------------


async def test_update_applies_mutation_and_persists(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    result = await store.update(lambda cfg: cfg.model_copy(update={"spotify_connect_name": "Den"}))
    assert result.spotify_connect_name == "Den"
    assert store.read().spotify_connect_name == "Den"


async def test_update_blocks_while_lock_is_held(tmp_path: Path) -> None:
    """A second `update()` must wait for a first one to finish, not interleave.

    Holds the store's lock directly (standing in for one writer's
    in-progress read-modify-write) and confirms a concurrent `update()`
    call — e.g. from the other writer, PairingService vs. a Settings-save
    PUT — does not proceed until the lock is released.
    """
    store = ConfigStore(tmp_path / "config.json")
    await store._lock.acquire()
    try:
        task = asyncio.create_task(
            store.update(lambda cfg: cfg.model_copy(update={"spotify_connect_name": "Den"}))
        )
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        store._lock.release()

    result = await task
    assert result.spotify_connect_name == "Den"


async def test_update_sequential_calls_preserve_both_fields(tmp_path: Path) -> None:
    """Two independent update() calls, each touching a different field, must
    not clobber each other — the RACE-01 scenario reduced to ConfigStore
    alone: a pairing persist (audio_sink_address) followed by a Settings
    save (spotify_connect_name) must leave both set.
    """
    store = ConfigStore(tmp_path / "config.json")

    await store.update(
        lambda cfg: cfg.model_copy(update={"audio_sink_address": "50:1B:6A:14:FD:1D"})
    )
    await store.update(lambda cfg: cfg.model_copy(update={"spotify_connect_name": "Den"}))

    final = store.read()
    assert final.audio_sink_address == "50:1B:6A:14:FD:1D"
    assert final.spotify_connect_name == "Den"
