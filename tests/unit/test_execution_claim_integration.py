import os
import asyncio
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from valueroute.domain.models import WorkerAttempt, WorkerAttemptStatus
from valueroute.execution.manager import ExecutionManager
from valueroute.storage.journal import LocalJournal
from valueroute.storage.store import Store
from valueroute.storage.checkpoints import Checkpoint, CheckpointStore


def test_claim_and_heartbeat_are_persisted_and_replayed(tmp_path):
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
    journal = LocalJournal(tmp_path)
    store = Store(journal)
    attempt = WorkerAttempt(id="wa_1", worker_session_id="ws_1", child_task_id="ct_1")
    store.attempts[attempt.id] = attempt
    store.attempt_session[attempt.id] = "cs_1"
    manager = ExecutionManager(store)
    claimed = manager.claim_attempt(attempt.id, now=moment, ttl=timedelta(seconds=30))
    renewed = manager.heartbeat(attempt.id, claimed.claim_token, now=moment + timedelta(seconds=5), ttl=timedelta(seconds=30))
    assert claimed.status is WorkerAttemptStatus.claimed
    assert renewed.claim_expires_at == moment + timedelta(seconds=35)
    journal.close()

    restarted_journal = LocalJournal(tmp_path)
    restarted = Store(restarted_journal)
    assert restarted.attempts[attempt.id].claim_expires_at == renewed.claim_expires_at
    restarted_journal.close()


def test_startup_reclaims_old_claim_only_with_safe_checkpoint(tmp_path):
    journal = LocalJournal(tmp_path)
    store = Store(journal)
    attempt = WorkerAttempt(id="wa_1", worker_session_id="ws_1", child_task_id="ct_1", checkpoint_id="cp_1")
    store.attempts[attempt.id] = attempt
    store.attempt_session[attempt.id] = "cs_1"
    manager = ExecutionManager(store)
    manager.claim_attempt(attempt.id, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    checkpoints = CheckpointStore(tmp_path)
    checkpoints.save(Checkpoint(id="cp_1", boundary_version=1, owner_version=1, safe_to_resume=True))
    journal.close()

    restarted_journal = LocalJournal(tmp_path)
    restarted = Store(restarted_journal, checkpoints)
    assert restarted.attempts[attempt.id].status is WorkerAttemptStatus.queued
    assert restarted.attempts[attempt.id].claim_token is None
    restarted_journal.close()


def test_startup_prefers_explicit_recovery_checkpoint_over_stale_current_checkpoint(tmp_path: Path):
    journal = LocalJournal(tmp_path)
    store = Store(journal)
    checkpoints = CheckpointStore(tmp_path)
    checkpoints.save(Checkpoint(id="cp_stale", boundary_version=1, owner_version=1, safe_to_resume=False))
    checkpoints.save(Checkpoint(id="cp_recovery", boundary_version=1, owner_version=1, safe_to_resume=True))
    attempt = WorkerAttempt(
        id="wa_lineage",
        worker_session_id="ws_1",
        child_task_id="ct_1",
        checkpoint_id="cp_stale",
        recovery_checkpoint_id="cp_recovery",
        checkpoint_safe_to_resume=True,
    )
    store.attempts[attempt.id] = attempt
    store.attempt_session[attempt.id] = "cs_1"
    claimed = ExecutionManager(store).claim_attempt(attempt.id, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert claimed.claim_token
    journal.close()

    restarted_journal = LocalJournal(tmp_path)
    restarted = Store(restarted_journal, checkpoints)
    try:
        assert restarted.attempts[attempt.id].status is WorkerAttemptStatus.queued
        assert restarted.attempts[attempt.id].recovery_checkpoint_id == "cp_recovery"
        assert restarted.attempts[attempt.id].claim_token is None
    finally:
        restarted_journal.close()


def test_sigkill_reclaims_old_claim_with_safe_checkpoint(tmp_path: Path):
    """A killed worker leaves only durable running state; restart requeues it."""
    journal = LocalJournal(tmp_path)
    store = Store(journal)
    checkpoints = CheckpointStore(tmp_path)
    checkpoints.save(Checkpoint(id="cp_safe", boundary_version=1, owner_version=1, safe_to_resume=True))
    attempt = WorkerAttempt(
        id="wa_killed",
        worker_session_id="ws_killed",
        child_task_id="ct_killed",
        checkpoint_id="cp_safe",
        checkpoint_safe_to_resume=True,
    )
    store.attempts[attempt.id] = attempt
    store.attempt_session[attempt.id] = "cs_killed"
    store.commit({"type": "worker.queued", "data": attempt.model_dump(mode="json") | {"session_id": "cs_killed"}})
    journal.close()

    source_root = Path(__file__).parents[2] / "src"
    ready_marker = tmp_path / "worker.ready"
    child_script = r'''
import sys
import time
from pathlib import Path
from valueroute.domain.models import WorkerAttemptStatus, WorkerAttemptTransitionConditions
from valueroute.domain.state_machine import transition_worker_attempt
from valueroute.execution.manager import ExecutionManager
from valueroute.storage.journal import LocalJournal
from valueroute.storage.store import Store

root = Path(sys.argv[1])
journal = LocalJournal(root)
store = Store(journal)
manager = ExecutionManager(store)
claimed = manager.claim_attempt("wa_killed")
running = transition_worker_attempt(
    claimed,
    WorkerAttemptStatus.running,
    claimed.version,
    conditions=WorkerAttemptTransitionConditions(claim_token_valid=True),
)
store.attempts["wa_killed"] = running
store.commit({"type": "worker.started", "data": running.model_dump(mode="json") | {"session_id": "cs_killed"}})
Path(root / "worker.ready").write_text("running", encoding="utf-8")
time.sleep(60)
'''
    environment = os.environ | {"PYTHONPATH": str(source_root)}
    child = subprocess.Popen(
        [sys.executable, "-c", child_script, str(tmp_path)],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    restarted_journal = None
    try:
        deadline = time.monotonic() + 5
        while not ready_marker.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready_marker.exists(), child.stderr.read() if child.poll() is not None else "worker did not reach durable running state"

        os.kill(child.pid, signal.SIGKILL)
        assert child.wait(timeout=5) == -signal.SIGKILL

        restarted_journal = LocalJournal(tmp_path)
        restarted = Store(restarted_journal, checkpoints)
        assert restarted.attempts[attempt.id].status is WorkerAttemptStatus.queued
        assert restarted.attempts[attempt.id].claim_token is None
        assert any(event["type"] == "worker.recovered" for event in restarted_journal.events())
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        if restarted_journal is not None:
            restarted_journal.close()


def test_sigkill_recovered_attempt_is_continued_by_supervisor(tmp_path: Path):
    """A killed provider call is reclaimed and executed from its durable request."""
    journal = LocalJournal(tmp_path)
    store = Store(journal)
    checkpoints = CheckpointStore(tmp_path)
    attempt = WorkerAttempt(id="wa_resume_provider", worker_session_id="ws", child_task_id="ct", checkpoint_safe_to_resume=False)
    store.attempts[attempt.id] = attempt
    store.attempt_session[attempt.id] = "cs"
    store.commit({"type": "worker.queued", "data": attempt.model_dump(mode="json") | {"session_id": "cs"}})
    journal.close()

    source_root = Path(__file__).parents[2] / "src"
    started = tmp_path / "provider.started"
    child_script = r'''
import asyncio
import sys
from pathlib import Path
from valueroute.execution.runner import WorkerRunner
from valueroute.storage.checkpoints import CheckpointStore
from valueroute.storage.journal import LocalJournal
from valueroute.storage.store import Store

root = Path(sys.argv[1])

class Provider:
    async def complete(self, **kwargs):
        (root / "provider.started").write_text("started", encoding="utf-8")
        await asyncio.Event().wait()

async def main():
    journal = LocalJournal(root)
    store = Store(journal)
    await WorkerRunner(store, Provider(), checkpoint_store=CheckpointStore(root), provider_timeout=None).run(
        "wa_resume_provider", task_id="task_resume", input_text="durable input"
    )

asyncio.run(main())
'''
    environment = os.environ | {"PYTHONPATH": str(source_root)}
    child = subprocess.Popen([sys.executable, "-c", child_script, str(tmp_path)], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 5
        while not started.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started.exists(), child.stderr.read() if child.poll() is not None else "provider did not start"
        os.kill(child.pid, signal.SIGKILL)
        assert child.wait(timeout=5) == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)

    restarted_journal = LocalJournal(tmp_path)
    restarted = Store(restarted_journal, checkpoints)
    assert restarted.attempts[attempt.id].status is WorkerAttemptStatus.queued
    assert restarted.attempts[attempt.id].provider_request is not None

    class CompletingProvider:
        async def complete(self, **kwargs):
            from valueroute.observability.usage import UsageRecord
            return type("Result", (), {"usage": UsageRecord(id="u_resume", task_id="task_resume", provider_id="test", model_id="m", latency_ms=1)})()

    from valueroute.execution.supervisor import ExecutionSupervisor
    result = asyncio.run(ExecutionSupervisor(restarted, CompletingProvider(), runner_kwargs={"checkpoint_store": checkpoints}).run_once())
    assert result is not None
    assert result.status is WorkerAttemptStatus.succeeded
    restarted_journal.close()
