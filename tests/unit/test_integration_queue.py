from pathlib import Path

import pytest

from valueroute.domain.models import IntegrationAttempt, IntegrationAttemptStatus
from valueroute.integration.queue import IntegrationQueue
from valueroute.storage.journal import LocalJournal
from valueroute.storage.store import Store


def _attempt(identifier: str = "ia-1") -> IntegrationAttempt:
    return IntegrationAttempt(id=identifier, client_ref=identifier, order_index=0)


def test_queue_claim_ack_and_replay(tmp_path: Path):
    journal = LocalJournal(tmp_path / "state")
    store = Store(journal)
    queue = IntegrationQueue(store)
    attempt = queue.enqueue(_attempt())
    claim = queue.claim(token="worker-a")
    assert claim is not None
    terminal = claim.attempt.model_copy(update={"status": IntegrationAttemptStatus.integrated, "version": 3, "revision": "r2"})
    queue.ack(claim, terminal)
    journal.close()

    restarted_journal = LocalJournal(tmp_path / "state")
    restarted = Store(restarted_journal)
    assert restarted.integration_attempts[attempt.id].status is IntegrationAttemptStatus.integrated
    restarted_journal.close()


def test_queue_requeue_recovers_running_work(tmp_path: Path):
    journal = LocalJournal(tmp_path / "state")
    store = Store(journal)
    queue = IntegrationQueue(store)
    queue.enqueue(_attempt())
    claim = queue.claim()
    assert claim is not None
    queue.requeue(claim, message="transient")
    assert store.integration_attempts["ia-1"].status is IntegrationAttemptStatus.queued
    assert IntegrationQueue(store).recover() == 0
    journal.close()


def test_queue_rejects_wrong_claim_owner(tmp_path: Path):
    journal = LocalJournal(tmp_path / "state")
    store = Store(journal)
    queue = IntegrationQueue(store)
    queue.enqueue(_attempt())
    claim = queue.claim(token="a")
    assert claim is not None
    with pytest.raises(ValueError, match="not_owned"):
        queue.requeue(type(claim)(claim.attempt, "b"))
    journal.close()
