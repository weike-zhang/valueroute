import asyncio
from dataclasses import dataclass

from valueroute.domain.models import WorkerAttempt, WorkerAttemptStatus
from valueroute.ownership.boundaries import ChildTaskBoundary
from valueroute.execution.supervisor import ExecutionRequest, ExecutionSupervisor
from valueroute.observability.usage import UsageRecord
from valueroute.storage.journal import LocalJournal
from valueroute.storage.store import Store


@dataclass
class ProviderResult:
    usage: UsageRecord


class Provider:
    async def complete(self, *, task_id: str, input_text: str, **kwargs):
        await asyncio.sleep(0.001)
        return ProviderResult(UsageRecord(id=f"u_{task_id}", task_id=task_id, provider_id="test", model_id="model", latency_ms=1))


def test_supervisor_claims_and_drains_registered_attempts(tmp_path):
    journal = LocalJournal(tmp_path)
    store = Store(journal)
    supervisor = ExecutionSupervisor(store, Provider(), max_concurrency=2)
    for index in range(2):
        attempt = WorkerAttempt(id=f"wa_{index}", worker_session_id=f"ws_{index}", child_task_id=f"ct_{index}")
        supervisor.runner._manager.queue.enqueue(attempt)
        supervisor.register(attempt.id, ExecutionRequest(task_id=f"task_{index}", input_text="work"))

    results = asyncio.run(supervisor.run_until_idle())

    assert {result.status for result in results} == {WorkerAttemptStatus.succeeded}
    assert {result.id for result in results} == {"wa_0", "wa_1"}
    assert all(attempt.status is WorkerAttemptStatus.succeeded for attempt in store.attempts.values())
    journal.close()


def test_supervisor_rejects_unbounded_concurrency(tmp_path):
    journal = LocalJournal(tmp_path)
    store = Store(journal)
    try:
        ExecutionSupervisor(store, Provider(), max_concurrency=6)
    except ValueError as error:
        assert "max_concurrency" in str(error)
    else:
        raise AssertionError("expected max_concurrency validation")
    journal.close()


def test_supervisor_reconstructs_minimal_request_from_durable_child(tmp_path):
    journal = LocalJournal(tmp_path)
    store = Store(journal)
    child = ChildTaskBoundary(id="child_durable", parent_task_id="parent", objective="resume this objective")
    store.children[child.id] = child
    attempt = WorkerAttempt(id="wa_durable", worker_session_id="ws", child_task_id=child.id)
    supervisor = ExecutionSupervisor(store, Provider())
    supervisor.runner._manager.queue.enqueue(attempt)

    result = asyncio.run(supervisor.run_once())

    assert result is not None
    assert result.status is WorkerAttemptStatus.succeeded
    assert store.usage[child.id][0].task_id == child.id
    journal.close()


def test_supervisor_replays_provider_request_after_restart(tmp_path):
    journal = LocalJournal(tmp_path)
    store = Store(journal)
    attempt = WorkerAttempt(id="wa_restart", worker_session_id="ws", child_task_id="ct_restart")
    supervisor = ExecutionSupervisor(store, Provider())
    supervisor.runner._manager.queue.enqueue(attempt)
    supervisor.register(attempt.id, ExecutionRequest(task_id="task_restart", input_text="durable work"))
    journal.close()

    restarted_journal = LocalJournal(tmp_path)
    restarted = Store(restarted_journal)
    assert restarted.attempts[attempt.id].provider_request is not None
    result = asyncio.run(ExecutionSupervisor(restarted, Provider()).run_once())
    assert result is not None
    assert result.status is WorkerAttemptStatus.succeeded
    restarted_journal.close()
