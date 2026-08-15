import os
import subprocess
import sys
from pathlib import Path

import pytest

from valueroute.api.app import create_app
from valueroute.storage.journal import InstanceLockedError, JournalRecoveryError, LocalJournal
from valueroute.storage.store import Store


def test_second_process_cannot_open_same_data_directory(tmp_path: Path):
    journal = LocalJournal(tmp_path)
    source_root = Path(__file__).parents[2] / "src"
    environment = os.environ | {"PYTHONPATH": str(source_root)}
    child = subprocess.run(
        [sys.executable, "-c", "from pathlib import Path; from valueroute.storage.journal import LocalJournal; LocalJournal(Path(__import__('sys').argv[1]))", str(tmp_path)],
        env=environment,
        capture_output=True,
        text=True,
    )
    try:
        assert child.returncode != 0
        assert "instance_locked" in child.stderr
        with pytest.raises(InstanceLockedError, match="instance_locked"):
            LocalJournal(tmp_path)
    finally:
        journal.close()


def test_incomplete_tail_is_quarantined_and_prior_frames_replay(tmp_path: Path):
    journal = LocalJournal(tmp_path)
    journal.append([{"type": "task.created", "data": {"id": "pt_1"}}])
    journal.close()
    journal_path = tmp_path / "journal" / "active.jsonl"
    journal_path.write_bytes(journal_path.read_bytes() + b'{"format_version":1,"records":')

    recovered = LocalJournal(tmp_path)
    try:
        assert recovered.events() == [{"type": "task.created", "data": {"id": "pt_1"}, "sequence": 1}]
        assert recovered.recovery_diagnostics[0]["code"] == "journal_tail_quarantined"
        quarantined = Path(recovered.recovery_diagnostics[0]["quarantine_path"])
        assert quarantined.read_bytes() == b'{"format_version":1,"records":'
        recovered.append([{"type": "task.updated", "data": {"id": "pt_1"}}])
    finally:
        recovered.close()
    reopened = LocalJournal(tmp_path)
    try:
        assert [event["type"] for event in reopened.events()] == ["task.created", "task.updated"]
    finally:
        reopened.close()


def test_non_tail_corruption_refuses_replay_with_repair_diagnostic(tmp_path: Path):
    journal = LocalJournal(tmp_path)
    journal.append([{"type": "first", "data": {}}])
    journal.append([{"type": "second", "data": {}}])
    journal.close()
    journal_path = tmp_path / "journal" / "active.jsonl"
    first, second = journal_path.read_bytes().splitlines(keepends=True)
    journal_path.write_bytes(first + b"broken-frame\n" + second)

    corrupted = journal_path.read_bytes()
    with pytest.raises(JournalRecoveryError, match=r"journal_corrupt_non_tail.*repair"):
        LocalJournal(tmp_path)
    assert journal_path.read_bytes() == corrupted

    # The service cannot expose a ready endpoint when replay is unsafe; startup
    # itself fails closed and leaves the damaged journal for explicit repair.
    with pytest.raises(JournalRecoveryError, match="journal_corrupt_non_tail"):
        create_app(tmp_path)
    assert not (tmp_path / "quarantine").exists()


def test_corrupt_checksummed_tail_is_quarantined_and_can_be_followed_by_append(tmp_path: Path):
    journal = LocalJournal(tmp_path)
    journal.append([{"type": "first", "data": {}}])
    journal.close()

    journal_path = tmp_path / "journal" / "active.jsonl"
    valid, = journal_path.read_bytes().splitlines(keepends=True)
    corrupt = valid.replace(b'"checksum":"', b'"checksum":"0', 1)
    journal_path.write_bytes(valid + corrupt)

    recovered = LocalJournal(tmp_path)
    try:
        assert [event["type"] for event in recovered.events()] == ["first"]
        assert recovered.recovery_diagnostics[0]["code"] == "journal_tail_quarantined"
        quarantined = Path(recovered.recovery_diagnostics[0]["quarantine_path"])
        assert quarantined.read_bytes() == corrupt
        recovered.append([{"type": "second", "data": {}}])
    finally:
        recovered.close()

    reopened = LocalJournal(tmp_path)
    try:
        assert [(event["sequence"], event["type"]) for event in reopened.events()] == [(1, "first"), (2, "second")]
    finally:
        reopened.close()


def test_snapshot_and_safe_compaction_replay_without_duplicate_events(tmp_path: Path):
    journal = LocalJournal(tmp_path)
    store = Store(journal)
    store.commit({"type": "task.created", "data": {"id": "pt_snapshot"}})
    first = store.snapshot()
    assert first["sequence"] == 1
    store.commit({"type": "task.updated", "data": {"id": "pt_snapshot"}})
    compacted = store.compact()
    assert compacted["compacted"] is False
    assert compacted["journal_retained"] is True
    journal.close()

    reopened_journal = LocalJournal(tmp_path)
    try:
        assert [event["type"] for event in reopened_journal.events()] == ["task.created", "task.updated"]
        assert reopened_journal.sequence == 2
    finally:
        reopened_journal.close()


def test_corrupt_newest_snapshot_falls_back_without_losing_retained_journal(tmp_path: Path):
    journal = LocalJournal(tmp_path)
    store = Store(journal)
    store.commit({"type": "first", "data": {}})
    store.snapshot()
    store.commit({"type": "second", "data": {}})
    newest = store.snapshot()["snapshot"]
    journal.close()

    snapshot_path = tmp_path / "snapshots" / newest
    snapshot_path.write_bytes(snapshot_path.read_bytes().replace(b'"payload_hash":"', b'"payload_hash":"0', 1))
    recovered = LocalJournal(tmp_path)
    try:
        assert [event["type"] for event in recovered.events()] == ["first", "second"]
        assert any(item["code"] == "snapshot_ignored" for item in recovered.recovery_diagnostics)
    finally:
        recovered.close()
