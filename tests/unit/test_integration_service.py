from pathlib import Path

from valueroute.domain.models import IntegrationAttempt, IntegrationAttemptStatus, ResourceRegion, WriterLease
from valueroute.integration.queue import IntegrationQueue
from valueroute.integration.service import IntegrationService
from valueroute.storage.journal import LocalJournal
from valueroute.storage.store import Store
from valueroute.workspaces.local import LocalWorkspaceAdapter


def _lease(owner: str, revision: str, path: str) -> WriterLease:
    return WriterLease(id=f"lease-{owner}", child_task_id="child", owner_agent_id=owner, region=ResourceRegion(resource_kind="file", resource_id=path, selector_type="whole_resource", selector_value="", base_revision=revision))


def test_integration_service_consumes_order_and_records_audit(tmp_path: Path):
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    (canonical / "a.txt").write_text("old")
    adapter = LocalWorkspaceAdapter(canonical, tmp_path / "workers")
    snapshot = adapter.snapshot()
    owner = adapter.create_owner_workspace("owner-a", snapshot)
    (owner / "a.txt").write_text("new")
    changeset = adapter.create_changeset("owner-a", owner, snapshot)
    events: list[dict] = []
    result = IntegrationService(adapter, events.append).integrate_in_order(["a"], {"a": changeset}, [_lease("owner-a", snapshot.revision, "a.txt")])
    assert result[0]["status"] == "integrated"
    assert (canonical / "a.txt").read_text() == "new"
    assert events[-1]["type"] == "integration.completed"
    adapter.cleanup_owner_workspace(owner)
    assert list((tmp_path / "workers").iterdir()) == []


def test_integration_attempts_are_journaled_and_recovered_in_plan_order(tmp_path: Path):
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    (canonical / "a.txt").write_text("old")
    adapter = LocalWorkspaceAdapter(canonical, tmp_path / "workers")
    snapshot = adapter.snapshot()
    owner = adapter.create_owner_workspace("owner-a", snapshot)
    (owner / "a.txt").write_text("new")
    changeset = adapter.create_changeset("owner-a", owner, snapshot)

    journal = LocalJournal(tmp_path / "state")
    store = Store(journal)
    service = IntegrationService(adapter, store=store, queue=IntegrationQueue(store))
    first = service.integrate_in_order(
        ["a", "missing"],
        {"a": changeset},
        [_lease("owner-a", snapshot.revision, "a.txt")],
        parent_task_id="task-1",
    )
    assert [item["status"] for item in first] == ["integrated", "blocked"]
    assert [item["client_ref"] for item in first] == ["a", "missing"]
    attempts = store.integration_attempts_for_order(["missing", "a"], "task-1")
    assert [attempt.client_ref for attempt in attempts] == ["missing", "a"]
    assert store.latest_integration_attempt("a", "task-1").status is IntegrationAttemptStatus.integrated
    journal.close()

    restarted_journal = LocalJournal(tmp_path / "state")
    restarted = Store(restarted_journal)
    assert restarted.recover_integration_results(["a", "missing"], "task-1") == first
    recovered = IntegrationService(adapter, store=restarted).integrate_in_order(
        ["a", "missing"],
        {"a": changeset},
        [_lease("owner-a", snapshot.revision, "a.txt")],
        parent_task_id="task-1",
    )
    assert recovered == first
    assert (canonical / "a.txt").read_text() == "new"
    adapter.cleanup_owner_workspace(owner)
    restarted_journal.close()


def test_store_replays_integration_attempt_transitions(tmp_path: Path):
    journal = LocalJournal(tmp_path)
    store = Store(journal)
    attempt = IntegrationAttempt(id="ia-1", client_ref="a", owner_id="owner-a", base_revision="r1")
    store.record_integration_attempt(attempt, "integration.queued")
    running = attempt.model_copy(update={"status": IntegrationAttemptStatus.running, "version": 2})
    store.record_integration_attempt(running, "integration.started")
    completed = running.model_copy(update={"status": IntegrationAttemptStatus.integrated, "version": 3, "revision": "r2"})
    store.record_integration_attempt(completed, "integration.completed")
    journal.close()

    restarted_journal = LocalJournal(tmp_path)
    restarted = Store(restarted_journal)
    assert restarted.integration_attempts[attempt.id] == completed
    assert restarted.recover_integration_results(["a"]) == [{"client_ref": "a", "status": "integrated", "owner_id": "owner-a", "revision": "r2"}]
    restarted_journal.close()
