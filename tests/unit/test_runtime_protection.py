from pathlib import Path

import pytest

from valueroute.settings import RuntimeProtectionConfig, RuntimeProtectionError, ensure_storage_capacity
from valueroute.storage.artifacts import ArtifactStore
from valueroute.storage.journal import LocalJournal


def test_runtime_protection_config_is_env_configurable(monkeypatch):
    monkeypatch.setenv("VALUEROUTE_MAX_ARTIFACT_BYTES", "123")
    monkeypatch.setenv("VALUEROUTE_CLAIM_TTL_SECONDS", "9")
    config = RuntimeProtectionConfig.from_environment()
    assert config.max_artifact_bytes == 123
    assert config.claim_ttl_seconds == 9


def test_artifact_size_limit_fails_closed(tmp_path: Path):
    store = ArtifactStore(tmp_path, max_bytes=3, min_free_bytes=0)
    with pytest.raises(RuntimeProtectionError, match="storage_limit_exceeded"):
        store.put(b"1234")


def test_journal_size_limit_fails_before_append(tmp_path: Path):
    journal = LocalJournal(tmp_path, max_bytes=1, min_free_bytes=0)
    try:
        with pytest.raises(RuntimeProtectionError, match="storage_limit_exceeded"):
            journal.append([{"type": "test", "data": {}}])
    finally:
        journal.close()


def test_ensure_storage_capacity_rejects_when_free_disk_is_below_threshold(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("valueroute.settings.shutil.disk_usage", lambda path: type("DU", (), {"free": 10 * 1024 * 1024})())
    with pytest.raises(RuntimeProtectionError, match="disk_free_space_below_threshold"):
        ensure_storage_capacity(tmp_path, incoming_bytes=5 * 1024 * 1024, max_bytes=None, min_free_bytes=100 * 1024 * 1024)


def test_ensure_storage_capacity_allows_when_free_disk_is_above_threshold(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("valueroute.settings.shutil.disk_usage", lambda path: type("DU", (), {"free": 200 * 1024 * 1024})())
    ensure_storage_capacity(tmp_path, incoming_bytes=5 * 1024 * 1024, max_bytes=None, min_free_bytes=100 * 1024 * 1024)


def test_ensure_storage_capacity_rejects_negative_incoming_bytes(tmp_path: Path):
    with pytest.raises(ValueError, match="must not be negative"):
        ensure_storage_capacity(tmp_path, incoming_bytes=-1, max_bytes=None, min_free_bytes=0)
