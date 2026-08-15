from pathlib import Path

import pytest

from valueroute.domain.errors import DomainError
from valueroute.domain.models import WorkerAttempt
from valueroute.execution.manager import ExecutionManager
from valueroute.storage.artifacts import ArtifactStore
from valueroute.storage.checkpoints import Checkpoint, CheckpointStore
from valueroute.storage.journal import LocalJournal
from valueroute.storage.store import Store


def test_recovery_creates_new_attempt_with_checkpoint_lineage(tmp_path: Path):
    journal = LocalJournal(tmp_path)
    store = Store(journal)
    checkpoints = CheckpointStore(tmp_path, ArtifactStore(tmp_path))
    checkpoints.save(Checkpoint(id="cp_safe", boundary_version=1, owner_version=1, safe_to_resume=True))
    previous = WorkerAttempt(id="wa_old", worker_session_id="ws", child_task_id="child", checkpoint_id="cp_safe", checkpoint_safe_to_resume=True)
    store.attempts[previous.id] = previous
    store.attempt_session[previous.id] = "session"
    recovery = ExecutionManager(store).recover_attempt(previous.id, checkpoints)
    assert recovery.resumed_from_attempt_id == previous.id
    assert recovery.recovery_checkpoint_id == "cp_safe"
    assert recovery.id in store.attempts and recovery.status.value == "queued"
    journal.close()


def test_recovery_rejects_unsafe_checkpoint(tmp_path: Path):
    journal = LocalJournal(tmp_path)
    store = Store(journal)
    checkpoints = CheckpointStore(tmp_path, ArtifactStore(tmp_path))
    checkpoints.save(Checkpoint(id="cp_unsafe", boundary_version=1, owner_version=1, safe_to_resume=False))
    previous = WorkerAttempt(id="wa_old", worker_session_id="ws", child_task_id="child", checkpoint_id="cp_unsafe", checkpoint_safe_to_resume=False)
    store.attempts[previous.id] = previous
    with pytest.raises(DomainError, match="not safe"):
        ExecutionManager(store).recover_attempt(previous.id, checkpoints)
    journal.close()
