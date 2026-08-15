from datetime import datetime, timezone

import pytest

from valueroute.domain.errors import DomainError
from valueroute.domain.models import WorkerAttempt, WorkerAttemptStatus
from valueroute.execution.queue import ExecutionQueue
from valueroute.storage.checkpoints import Checkpoint, CheckpointStore
from valueroute.storage.journal import LocalJournal
from valueroute.storage.store import Store


def test_queue_claim_requeue_and_terminal_ack_replay(tmp_path):
    journal = LocalJournal(tmp_path)
    store = Store(journal)
    checkpoints = CheckpointStore(tmp_path)
    checkpoints.save(Checkpoint(id="cp_safe", boundary_version=1, owner_version=1, safe_to_resume=True))
    queue = ExecutionQueue(store)
    attempt = WorkerAttempt(
        id="wa_queue",
        worker_session_id="ws_queue",
        child_task_id="ct_queue",
        checkpoint_id="cp_safe",
        checkpoint_safe_to_resume=True,
    )

    queue.enqueue(attempt, session_id="cs_queue")
    claimed = queue.claim(attempt.id, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert claimed.status is WorkerAttemptStatus.claimed
    requeued = queue.requeue(attempt.id, checkpoint_store=checkpoints)
    assert requeued.status is WorkerAttemptStatus.queued

    terminal = requeued.model_copy(update={"status": WorkerAttemptStatus.succeeded, "version": requeued.version + 1})
    store.commit({"type": "worker.stopped", "data": terminal.model_dump(mode="json") | {"session_id": "cs_queue"}})
    store.attempts[attempt.id] = terminal
    queue.ack_terminal(attempt.id)
    journal.close()

    restarted_journal = LocalJournal(tmp_path)
    restarted = Store(restarted_journal)
    assert restarted.attempts[attempt.id].status is WorkerAttemptStatus.succeeded
    assert attempt.id in restarted.terminal_acks
    restarted_journal.close()


def test_queue_rejects_unsafe_requeue(tmp_path):
    journal = LocalJournal(tmp_path)
    store = Store(journal)
    queue = ExecutionQueue(store)
    attempt = WorkerAttempt(id="wa_unsafe", worker_session_id="ws", child_task_id="ct")
    queue.enqueue(attempt)
    queue.claim(attempt.id)

    with pytest.raises(DomainError, match="safe recovery checkpoint"):
        queue.requeue(attempt.id)
    journal.close()
