import asyncio
from dataclasses import dataclass

from valueroute.domain.models import WorkerAttempt, WorkerAttemptStatus, WorkspaceBinding
from valueroute.execution.runner import WorkerRunner
from valueroute.execution.manager import ExecutionManager
from valueroute.observability.usage import UsageRecord
from valueroute.storage.journal import LocalJournal
from valueroute.storage.store import Store
from valueroute.storage.artifacts import ArtifactStore
from valueroute.storage.checkpoints import CheckpointStore


@dataclass
class ProviderResult:
    usage: UsageRecord


def usage() -> UsageRecord:
    return UsageRecord(id="usage_1", task_id="task_1", provider_id="test", model_id="model", latency_ms=1)


def attempt_store(tmp_path):
    journal = LocalJournal(tmp_path)
    store = Store(journal)
    attempt = WorkerAttempt(id="attempt_1", worker_session_id="session_1", child_task_id="child_1")
    store.attempts[attempt.id] = attempt
    store.attempt_session[attempt.id] = "controller_1"
    return store, journal, attempt


def test_runner_claims_calls_provider_reports_usage_and_succeeds(tmp_path):
    class Provider:
        async def complete(self, **kwargs):
            assert kwargs["task_id"] == "task_1"
            assert kwargs["input_text"] == "do work"
            return ProviderResult(usage())

    store, journal, attempt = attempt_store(tmp_path)
    received = []
    def record(value):
        received.append(value)
        store.record_usage(value)
    result = asyncio.run(WorkerRunner(store, Provider(), on_usage=record).run(attempt.id, task_id="task_1", input_text="do work"))

    assert result.status is WorkerAttemptStatus.succeeded
    assert len(received) == 1
    assert received[0].id == "usage_1"
    assert store.usage["task_1"][0].id == "usage_1"
    assert store.attempts[attempt.id].status is WorkerAttemptStatus.succeeded
    assert store.attempts[attempt.id].claim_token is not None
    journal.close()


def test_runner_maps_provider_failure_to_failed_without_reporting_usage(tmp_path):
    class Provider:
        async def complete(self, **kwargs):
            raise RuntimeError("provider unavailable")

    store, journal, attempt = attempt_store(tmp_path)
    received = []
    result = asyncio.run(WorkerRunner(store, Provider(), on_usage=received.append).run(attempt.id, task_id="task_1", input_text="do work"))

    assert result.status is WorkerAttemptStatus.failed
    assert result.status is not WorkerAttemptStatus.succeeded
    assert received == []
    journal.close()


def test_runner_maps_provider_timeout_to_failed(tmp_path):
    class Provider:
        async def complete(self, **kwargs):
            await asyncio.sleep(0.1)
            return ProviderResult(usage())

    store, journal, attempt = attempt_store(tmp_path)
    result = asyncio.run(WorkerRunner(store, Provider(), provider_timeout=0.001).run(attempt.id, task_id="task_1", input_text="do work"))

    assert result.status is WorkerAttemptStatus.failed
    journal.close()


def test_runner_persists_provider_boundary_checkpoint(tmp_path):
    class Provider:
        async def complete(self, **kwargs):
            return ProviderResult(usage())

    store, journal, attempt = attempt_store(tmp_path)
    checkpoints = CheckpointStore(tmp_path, ArtifactStore(tmp_path))
    result = asyncio.run(WorkerRunner(store, Provider(), checkpoint_store=checkpoints).run(attempt.id, task_id="task_1", input_text="do work"))
    assert result.status is WorkerAttemptStatus.succeeded
    persisted = store.attempts[attempt.id]
    assert persisted.checkpoint_id is not None
    assert persisted.checkpoint_safe_to_resume is True
    assert checkpoints.load(persisted.checkpoint_id).safe_to_resume is True
    journal.close()


def test_runner_passes_only_owner_workspace_to_provider(tmp_path):
    seen = {}

    class Provider:
        async def complete(self, **kwargs):
            seen.update(kwargs)
            return ProviderResult(usage())

    store, journal, attempt = attempt_store(tmp_path)
    store.attempts[attempt.id] = attempt.model_copy(update={"workspace": WorkspaceBinding(owner_id="owner", owner_workspace=str(tmp_path / "owner"), canonical_uri="/canonical", base_revision="r1")})
    result = asyncio.run(WorkerRunner(store, Provider()).run(attempt.id, task_id="task_1", input_text="do work"))
    assert result.status is WorkerAttemptStatus.succeeded
    assert seen["workspace_path"] == str(tmp_path / "owner")
    assert "canonical_uri" not in seen
    journal.close()


def test_runner_pause_request_stops_at_provider_boundary_and_persists_checkpoint(tmp_path):
    class Provider:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def complete(self, **kwargs):
            self.started.set()
            await self.release.wait()
            return ProviderResult(usage())

    store, journal, attempt = attempt_store(tmp_path)
    checkpoints = CheckpointStore(tmp_path, ArtifactStore(tmp_path))
    provider = Provider()

    async def exercise():
        running = asyncio.create_task(WorkerRunner(store, provider, checkpoint_store=checkpoints, provider_timeout=None).run(attempt.id, task_id="task_1", input_text="do work"))
        await provider.started.wait()
        requested = ExecutionManager(store).request_control(attempt.id, "pause")
        assert requested.status is WorkerAttemptStatus.pause_requested
        provider.release.set()
        return await running

    result = asyncio.run(exercise())
    assert result.status is WorkerAttemptStatus.paused
    assert result.checkpoint_safe_to_resume is True
    assert checkpoints.load(result.checkpoint_id).safe_to_resume is True
    journal.close()


def test_runner_cancel_request_uses_provider_hook_and_grace_period(tmp_path):
    class Provider:
        def __init__(self):
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def complete(self, **kwargs):
            self.started.set()
            await self.cancelled.wait()
            return ProviderResult(usage())

        async def cancel(self, **kwargs):
            self.cancelled.set()

    store, journal, attempt = attempt_store(tmp_path)
    provider = Provider()

    async def exercise():
        running = asyncio.create_task(
            WorkerRunner(store, provider, provider_timeout=None, cancel_grace_seconds=0.01).run(
                attempt.id, task_id="task_1", input_text="do work"
            )
        )
        await provider.started.wait()
        requested = ExecutionManager(store).request_control(attempt.id, "cancel")
        assert requested.status is WorkerAttemptStatus.cancel_requested
        return await running

    result = asyncio.run(exercise())
    assert result.status is WorkerAttemptStatus.cancelled
    assert provider.cancelled.is_set()
    journal.close()


def test_runner_does_not_claim_cancelled_when_provider_cannot_stop(tmp_path):
    class Provider:
        def __init__(self):
            self.started = asyncio.Event()

        async def complete(self, **kwargs):
            self.started.set()
            await asyncio.Event().wait()

    store, journal, attempt = attempt_store(tmp_path)
    provider = Provider()

    async def exercise():
        running = asyncio.create_task(
            WorkerRunner(store, provider, provider_timeout=None, cancel_grace_seconds=0.01).run(
                attempt.id, task_id="task_1", input_text="do work"
            )
        )
        await provider.started.wait()
        requested = ExecutionManager(store).request_control(attempt.id, "cancel")
        assert requested.status is WorkerAttemptStatus.cancel_requested
        return await running

    result = asyncio.run(exercise())
    assert result.status is WorkerAttemptStatus.failed
    journal.close()
