from __future__ import annotations

from collections.abc import Mapping

from valueroute.domain.models import (
    ObservationStatus,
    ParentCompletionEvidence,
    ParentTask,
    TaskStatus,
    WorkerAttempt,
    WorkerAttemptStatus,
    WorkerAttemptTransitionConditions,
    now,
)


class StateTransitionError(ValueError):
    """Raised when a requested lifecycle transition violates the domain contract."""


TASK_TERMINAL = frozenset({
    TaskStatus.completed,
    TaskStatus.partial,
    TaskStatus.blocked,
    TaskStatus.failed,
    TaskStatus.cancelled,
})
WORKER_ATTEMPT_TERMINAL = frozenset({
    WorkerAttemptStatus.succeeded,
    WorkerAttemptStatus.partial,
    WorkerAttemptStatus.blocked,
    WorkerAttemptStatus.failed,
    WorkerAttemptStatus.cancelled,
})

TASK_TRANSITIONS: Mapping[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.draft: frozenset({TaskStatus.planned}),
    TaskStatus.planned: frozenset({TaskStatus.queued}),
    TaskStatus.queued: frozenset({TaskStatus.running}),
    TaskStatus.running: frozenset({
        TaskStatus.waiting_approval,
        TaskStatus.pause_requested,
        TaskStatus.cancel_requested,
        TaskStatus.completed,
        TaskStatus.partial,
        TaskStatus.blocked,
        TaskStatus.failed,
    }),
    TaskStatus.waiting_approval: frozenset({TaskStatus.running, TaskStatus.cancelled, TaskStatus.blocked}),
    TaskStatus.pause_requested: frozenset({TaskStatus.paused, TaskStatus.failed}),
    TaskStatus.cancel_requested: frozenset({TaskStatus.cancelled, TaskStatus.failed}),
    TaskStatus.paused: frozenset({TaskStatus.queued, TaskStatus.cancelled}),
}

WORKER_ATTEMPT_TRANSITIONS: Mapping[WorkerAttemptStatus, frozenset[WorkerAttemptStatus]] = {
    WorkerAttemptStatus.queued: frozenset({WorkerAttemptStatus.claimed}),
    WorkerAttemptStatus.claimed: frozenset({WorkerAttemptStatus.running}),
    WorkerAttemptStatus.running: frozenset({
        WorkerAttemptStatus.waiting_approval,
        WorkerAttemptStatus.pause_requested,
        WorkerAttemptStatus.cancel_requested,
        WorkerAttemptStatus.succeeded,
        WorkerAttemptStatus.partial,
        WorkerAttemptStatus.blocked,
        WorkerAttemptStatus.failed,
    }),
    WorkerAttemptStatus.waiting_approval: frozenset({WorkerAttemptStatus.running, WorkerAttemptStatus.cancelled, WorkerAttemptStatus.failed}),
    WorkerAttemptStatus.pause_requested: frozenset({WorkerAttemptStatus.paused, WorkerAttemptStatus.failed}),
    WorkerAttemptStatus.cancel_requested: frozenset({WorkerAttemptStatus.cancelled, WorkerAttemptStatus.failed}),
    WorkerAttemptStatus.paused: frozenset({WorkerAttemptStatus.queued, WorkerAttemptStatus.cancelled}),
}


def _validate_expected_version(actual: int, expected: int) -> None:
    if actual != expected:
        raise StateTransitionError(f"version conflict: expected {expected}, got {actual}")


def _validate_transition(current: object, target: object, transitions: Mapping[object, frozenset[object]], terminal: frozenset[object]) -> None:
    if current in terminal:
        raise StateTransitionError(f"terminal state {current.value} cannot transition")
    if target not in transitions.get(current, frozenset()):
        raise StateTransitionError(f"invalid transition: {current.value} -> {target.value}")


def required_acceptance_is_satisfied(task: ParentTask) -> bool:
    """Check the latest observation for each required acceptance item."""
    latest = {}
    for record in task.evidence:
        previous = latest.get(record.requirement_id)
        if previous is None or record.recorded_at >= previous.recorded_at:
            latest[record.requirement_id] = record
    return all(
        not acceptance.required
        or ((record := latest.get(acceptance.id)) is not None
        and record.observation_status in {ObservationStatus.observed_pass, ObservationStatus.not_applicable})
        for acceptance in task.acceptance_contract
    )


def parent_completion_is_supported(task: ParentTask, evidence: ParentCompletionEvidence | None) -> bool:
    return bool(
        evidence
        and required_acceptance_is_satisfied(task)
        and evidence.changes_integrated
        and evidence.parent_verification_passed
        and all(status == TaskStatus.completed for status in evidence.child_statuses)
    )


def transition_task(task: ParentTask, target: TaskStatus, expected_version: int, *, completion_evidence: ParentCompletionEvidence | None = None) -> ParentTask:
    _validate_expected_version(task.version, expected_version)
    _validate_transition(task.status, target, TASK_TRANSITIONS, TASK_TERMINAL)
    if target == TaskStatus.completed and not parent_completion_is_supported(task, completion_evidence):
        raise StateTransitionError("parent task completion requires required acceptance evidence, completed children, integrated changes, and parent verification")
    return task.model_copy(update={"status": target, "version": task.version + 1, "updated_at": now()})


def transition_worker_attempt(
    attempt: WorkerAttempt,
    target: WorkerAttemptStatus,
    expected_version: int,
    *,
    conditions: WorkerAttemptTransitionConditions | None = None,
) -> WorkerAttempt:
    _validate_expected_version(attempt.version, expected_version)
    _validate_transition(attempt.status, target, WORKER_ATTEMPT_TRANSITIONS, WORKER_ATTEMPT_TERMINAL)
    conditions = conditions or WorkerAttemptTransitionConditions()
    if attempt.status == WorkerAttemptStatus.queued and target == WorkerAttemptStatus.claimed and not conditions.capacity_available:
        raise StateTransitionError("claim requires available worker capacity")
    if attempt.status == WorkerAttemptStatus.claimed and target == WorkerAttemptStatus.running and not conditions.claim_token_valid:
        raise StateTransitionError("running requires a valid claim token")
    if attempt.status == WorkerAttemptStatus.pause_requested and target == WorkerAttemptStatus.paused and not conditions.checkpoint_durable:
        raise StateTransitionError("pausing requires a durable checkpoint")
    if attempt.status == WorkerAttemptStatus.cancel_requested and target == WorkerAttemptStatus.cancelled and not conditions.execution_stopped:
        raise StateTransitionError("cancellation requires execution to stop")
    return attempt.model_copy(update={"status": target, "version": attempt.version + 1, "updated_at": now()})
