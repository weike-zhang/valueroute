import pytest

from valueroute.domain.errors import DomainError
from valueroute.domain.models import (
    Acceptance,
    ControllerEpoch,
    ControllerSession,
    OrchestrationMode,
    ParentTask,
    TaskStatus,
    WorkerAttempt,
    WorkerAttemptStatus,
    Workspace,
)
from valueroute.egress import EgressLedger, EgressPolicy, HandoffService
from valueroute.storage.journal import LocalJournal
from valueroute.storage.store import Store


def _store_with_attempt(tmp_path, *, mode=OrchestrationMode.worker_only, status=WorkerAttemptStatus.queued, session_provider="openai"):
    journal = LocalJournal(tmp_path)
    store = Store(journal)
    session = ControllerSession(id="cs_1", tenant_id="t", host_session_id="h", orchestration_mode=mode)
    epoch = ControllerEpoch(id="ce_1", controller_session_id="cs_1", provider_id=session_provider, model_id="gpt-a", reasoning_effort="low")
    store.sessions[session.id] = session
    store.epochs[epoch.id] = epoch
    store.sessions[session.id] = session.model_copy(update={"active_controller_epoch_id": epoch.id, "version": 2})
    child = _child("ct_1", "pt_1")
    store.children[child.id] = child
    task = ParentTask(
        id="pt_1",
        controller_session_id="cs_1",
        request_type="new_task",
        goal="do work",
        acceptance_contract=[Acceptance(id="a1", description="done")],
        data_classification="internal",
        workspace=Workspace(canonical_uri="workspace://x", base_revision="r1"),
        status=TaskStatus.running,
        child_task_ids=["ct_1"],
    )
    store.tasks[task.id] = task
    store.child_ref_ids[("pt_1", "ct_1")] = child.id
    attempt = WorkerAttempt(id="wa_1", worker_session_id="ws_1", child_task_id="ct_1", status=status)
    store.attempts[attempt.id] = attempt
    return store, journal, attempt


def _child(child_id, parent_id):
    from valueroute.ownership.boundaries import ChildTaskBoundary

    return ChildTaskBoundary(id=child_id, parent_task_id=parent_id, objective="objective")


def test_handoff_records_egress_and_repaints_attempt(tmp_path):
    store, journal, _ = _store_with_attempt(tmp_path)
    service = HandoffService(store)
    result = service.handoff_attempt(
        "wa_1",
        target_provider="anthropic",
        target_model="claude-x",
        fields=["task_id", "goal"],
        data_classification="internal",
    )
    assert result["target_provider"] == "anthropic"
    assert result["source_provider"] == "openai"
    assert result["mode"] == "read_only_handoff"
    assert store.attempts["wa_1"].provider_id == "anthropic"
    assert store.attempts["wa_1"].model_id == "claude-x"
    records = service.ledger.list(task_id="pt_1")
    assert len(records) == 1
    assert records[0].target_provider == "anthropic"
    assert records[0].data_classification == "internal"
    journal.close()


def test_handoff_denied_when_classification_not_allowed(tmp_path):
    store, journal, _ = _store_with_attempt(tmp_path)
    service = HandoffService(store)
    with pytest.raises(DomainError, match="egress policy does not allow"):
        service.handoff_attempt(
            "wa_1",
            target_provider="anthropic",
            target_model="claude-x",
            fields=["task_id"],
            data_classification="confidential",
        )
    assert len(service.ledger.list()) == 0
    journal.close()


def test_handoff_denied_for_target_provider_outside_policy(tmp_path):
    store, journal, _ = _store_with_attempt(tmp_path)
    policy = EgressPolicy(allowed_target_providers=["openai"])
    service = HandoffService(store, policy=policy)
    with pytest.raises(DomainError, match="egress policy does not allow"):
        service.handoff_attempt(
            "wa_1",
            target_provider="anthropic",
            target_model="claude-x",
            fields=["task_id"],
            data_classification="internal",
        )
    journal.close()


def test_handoff_denied_for_field_outside_allowed_prefixes(tmp_path):
    store, journal, _ = _store_with_attempt(tmp_path)
    service = HandoffService(store)
    with pytest.raises(DomainError, match="egress policy does not allow"):
        service.handoff_attempt(
            "wa_1",
            target_provider="anthropic",
            target_model="claude-x",
            fields=["task_id", "secret_token"],
            data_classification="internal",
        )
    journal.close()


def test_handoff_unknown_attempt_raises_not_found(tmp_path):
    store, journal, _ = _store_with_attempt(tmp_path)
    service = HandoffService(store)
    with pytest.raises(DomainError, match="worker attempt not found"):
        service.handoff_attempt("missing", target_provider="x", target_model="y", fields=[], data_classification="internal")
    journal.close()


def test_egress_ledger_replays_across_restart(tmp_path):
    store, journal, _ = _store_with_attempt(tmp_path)
    service = HandoffService(store)
    service.handoff_attempt("wa_1", target_provider="anthropic", target_model="claude-x", fields=["task_id"], data_classification="internal")
    journal.close()

    replayed_journal = LocalJournal(tmp_path)
    replayed = Store(replayed_journal)
    ledger = EgressLedger(replayed)
    records = ledger.list()
    assert len(records) == 1
    assert records[0].target_provider == "anthropic"
    assert replayed.attempts["wa_1"].provider_id == "anthropic"
    replayed_journal.close()


def test_egress_policy_default_allows_only_public_internal(tmp_path):
    policy = EgressPolicy()
    assert policy.allows("public", target_provider="anthropic", fields=["task_id"])
    assert policy.allows("internal", target_provider="anthropic", fields=["goal"])
    assert not policy.allows("confidential", target_provider="anthropic", fields=["task_id"])
    assert not policy.allows("restricted", target_provider="anthropic", fields=["task_id"])


def test_egress_policy_empty_allowed_targets_means_any(tmp_path):
    policy = EgressPolicy(allowed_target_providers=[])
    assert policy.allows("internal", target_provider="anything", fields=["task_id"])


def test_egress_policy_field_prefix_required(tmp_path):
    policy = EgressPolicy()
    assert policy.allows("internal", target_provider="anthropic", fields=["task_id", "goal", "acceptance"])
    assert not policy.allows("internal", target_provider="anthropic", fields=["billing_amount"])
