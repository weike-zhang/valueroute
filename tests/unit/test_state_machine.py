import pytest

from valueroute.domain.models import (
    Acceptance,
    EvidenceRecord,
    ObservationStatus,
    ParentCompletionEvidence,
    ParentTask,
    TaskStatus,
    WorkerAttempt,
    WorkerAttemptStatus,
    WorkerAttemptTransitionConditions,
    Workspace,
)
from valueroute.domain.state_machine import StateTransitionError, transition_task, transition_worker_attempt


def task(*, status=TaskStatus.draft, evidence=None):
    return ParentTask(
        id="pt_1",
        controller_session_id="cs_1",
        request_type="new_task",
        goal="test the state machine",
        acceptance_contract=[Acceptance(id="acceptance_1", description="the required result")],
        data_classification="internal",
        workspace=Workspace(canonical_uri="workspace://test", base_revision="r1"),
        status=status,
        evidence=evidence or [],
    )


def attempt(*, status=WorkerAttemptStatus.queued):
    return WorkerAttempt(id="wa_1", worker_session_id="ws_1", child_task_id="ct_1", status=status)


def test_task_transitions_require_expected_version_and_follow_lifecycle():
    planned = transition_task(task(), TaskStatus.planned, 1)
    queued = transition_task(planned, TaskStatus.queued, 2)
    assert transition_task(queued, TaskStatus.running, 3).status is TaskStatus.running
    with pytest.raises(StateTransitionError, match="version conflict"):
        transition_task(task(), TaskStatus.planned, 2)


def test_task_terminal_states_cannot_be_reopened_or_rewritten():
    completed = task(status=TaskStatus.completed)
    for target in (TaskStatus.running, TaskStatus.partial, TaskStatus.completed):
        with pytest.raises(StateTransitionError, match="terminal state"):
            transition_task(completed, target, 1)


def test_worker_attempt_transitions_and_terminal_states_are_immutable():
    claimed = transition_worker_attempt(attempt(), WorkerAttemptStatus.claimed, 1, conditions=WorkerAttemptTransitionConditions(capacity_available=True))
    running = transition_worker_attempt(claimed, WorkerAttemptStatus.running, 2, conditions=WorkerAttemptTransitionConditions(claim_token_valid=True))
    succeeded = transition_worker_attempt(running, WorkerAttemptStatus.succeeded, 3)
    assert succeeded.version == 4
    with pytest.raises(StateTransitionError, match="terminal state"):
        transition_worker_attempt(succeeded, WorkerAttemptStatus.queued, 4)


def test_worker_attempt_rejects_skipped_lifecycle_steps():
    with pytest.raises(StateTransitionError, match="invalid transition"):
        transition_worker_attempt(attempt(), WorkerAttemptStatus.running, 1)


def test_worker_attempt_guarded_transitions_require_documented_facts():
    with pytest.raises(StateTransitionError, match="available worker capacity"):
        transition_worker_attempt(attempt(), WorkerAttemptStatus.claimed, 1)

    pausing = attempt(status=WorkerAttemptStatus.pause_requested)
    with pytest.raises(StateTransitionError, match="durable checkpoint"):
        transition_worker_attempt(pausing, WorkerAttemptStatus.paused, 1)
    assert transition_worker_attempt(
        pausing,
        WorkerAttemptStatus.paused,
        1,
        conditions=WorkerAttemptTransitionConditions(checkpoint_durable=True),
    ).status is WorkerAttemptStatus.paused

    cancelling = attempt(status=WorkerAttemptStatus.cancel_requested)
    with pytest.raises(StateTransitionError, match="execution to stop"):
        transition_worker_attempt(cancelling, WorkerAttemptStatus.cancelled, 1)


def test_parent_completion_requires_required_evidence_children_integration_and_verification():
    running = task(status=TaskStatus.running)
    evidence = ParentCompletionEvidence(child_statuses=[TaskStatus.completed], changes_integrated=True, parent_verification_passed=True)
    with pytest.raises(StateTransitionError, match="requires required acceptance evidence"):
        transition_task(running, TaskStatus.completed, 1, completion_evidence=evidence)

    observed = running.model_copy(update={"evidence": [EvidenceRecord(id="ev_1", requirement_id="acceptance_1", evidence_type="test", observation_status=ObservationStatus.observed_pass, source="pytest")]})
    incomplete_children = evidence.model_copy(update={"child_statuses": [TaskStatus.partial]})
    with pytest.raises(StateTransitionError, match="requires required acceptance evidence"):
        transition_task(observed, TaskStatus.completed, 1, completion_evidence=incomplete_children)

    completed = transition_task(observed, TaskStatus.completed, 1, completion_evidence=evidence)
    assert completed.status is TaskStatus.completed


def test_unobserved_required_acceptance_blocks_parent_completion():
    running = task(
        status=TaskStatus.running,
        evidence=[EvidenceRecord(id="ev_1", requirement_id="acceptance_1", evidence_type="live_check", observation_status=ObservationStatus.unobserved, source="browser")],
    )
    evidence = ParentCompletionEvidence(child_statuses=[], changes_integrated=True, parent_verification_passed=True)
    with pytest.raises(StateTransitionError, match="requires required acceptance evidence"):
        transition_task(running, TaskStatus.completed, 1, completion_evidence=evidence)


def test_terminal_states_stay_immutable_after_journal_replay(tmp_path):
    from valueroute.storage.journal import LocalJournal
    from valueroute.storage.store import Store

    journal = LocalJournal(tmp_path)
    store = Store(journal)
    completed_task = task(status=TaskStatus.completed)
    failed_attempt = attempt(status=WorkerAttemptStatus.failed)
    store.tasks[completed_task.id] = completed_task
    store.attempts[failed_attempt.id] = failed_attempt
    store.commit({"type": "task.created", "data": completed_task.model_dump(mode="json")})
    store.commit({"type": "worker.terminal_ack", "data": failed_attempt.model_dump(mode="json")})
    journal.close()

    restarted_journal = LocalJournal(tmp_path)
    restarted = Store(restarted_journal)
    replayed_task = restarted.tasks[completed_task.id]
    replayed_attempt = restarted.attempts[failed_attempt.id]
    assert replayed_task.status is TaskStatus.completed
    assert replayed_attempt.status is WorkerAttemptStatus.failed
    for target in (TaskStatus.running, TaskStatus.partial, TaskStatus.completed):
        with pytest.raises(StateTransitionError, match="terminal state"):
            transition_task(replayed_task, target, replayed_task.version)
    for target in (WorkerAttemptStatus.queued, WorkerAttemptStatus.running, WorkerAttemptStatus.failed):
        with pytest.raises(StateTransitionError, match="terminal state"):
            transition_worker_attempt(replayed_attempt, target, replayed_attempt.version)
    restarted_journal.close()
