from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlparse

from valueroute.domain.errors import DomainError
from valueroute.domain.models import (
    ParentTask,
    WorkerAttempt,
    WorkerAttemptStatus,
    WorkerPlan,
    WorkspaceBinding,
    new_id,
)
from valueroute.domain.state_machine import StateTransitionError, transition_worker_attempt
from valueroute.execution.admission import WorkerAdmission
from valueroute.execution.claims import AttemptRecord, WorkerAttemptClaim, WorkerClaimService
from valueroute.execution.coordination import request_execution_control
from valueroute.execution.interfaces import ExecutionQueue
from valueroute.execution.queue import LocalExecutionQueue
from valueroute.storage.checkpoints import CheckpointStore
from valueroute.storage.interfaces import StateStore
from valueroute.workspaces.interfaces import WorkspaceAdapter


class ExecutionManager:
    """Durable attempt admission and local queue coordination.

    The manager exposes queue operations, while provider execution remains an
    explicit runner/supervisor concern rather than an implicit background task.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        queue: ExecutionQueue | None = None,
        workspace_adapter: WorkspaceAdapter | None = None,
    ):
        self.store = store
        self.workspace_adapter = workspace_adapter
        # The local queue remains the compatibility default.  Callers that
        # need a remote or test queue provide it explicitly; the manager never
        # relies on LocalExecutionQueue's concrete API beyond the protocol.
        self.queue: ExecutionQueue = queue or LocalExecutionQueue(store)

    def enqueue_plan(
        self,
        task: ParentTask,
        plan: WorkerPlan | None,
        *,
        actor_role: str = "controller",
        parent_depth: int = 0,
    ) -> list[WorkerAttempt]:
        if plan is None:
            return []
        WorkerAdmission(
            actor_role=actor_role,
            parent_depth=parent_depth,
            requested_workers=len(plan.children),
        ).validate()
        self.validate_capacity(task, plan)
        session = self.store.sessions[task.controller_session_id]
        attempts = []
        for child in plan.children:
            child_task_id = self.store.child_ref_ids.get((task.id, child.client_ref), child.client_ref)
            attempt = WorkerAttempt(
                id=new_id("wa"),
                worker_session_id=new_id("ws"),
                child_task_id=child_task_id,
                workspace=self._bind_workspace(task, child_task_id),
            )
            attempts.append(self.queue.enqueue(attempt, session_id=session.id))
        return attempts

    def _bind_workspace(self, task: ParentTask, child_task_id: str) -> WorkspaceBinding | None:
        """Give an attempt an isolated copy; never hand it the canonical root."""
        adapter = self.workspace_adapter
        if adapter is None:
            parsed = urlparse(task.workspace.canonical_uri)
            if parsed.scheme not in {"", "file"}:
                return None
            canonical = Path(unquote(parsed.path if parsed.scheme == "file" else task.workspace.canonical_uri)).expanduser().resolve()
            if not canonical.is_dir():
                return None
            from valueroute.workspaces.local import LocalWorkspaceAdapter

            adapter = LocalWorkspaceAdapter(canonical, canonical.parent / ".valueroute-workspaces")
        owner = self.store.assignments.get(child_task_id)
        owner_id = owner.owner_agent_id if owner is not None and owner.status == "active" else None
        # The attempt id is not available until WorkerAttempt construction. A
        # child without an assignment therefore receives a unique execution
        # owner below, after the attempt is created.
        if owner_id is None:
            owner_id = new_id("owner")
        bound = adapter.bind_owner_workspace(owner_id, base_revision=task.workspace.base_revision)
        return WorkspaceBinding(
            owner_id=owner_id,
            owner_workspace=str(bound.path),
            canonical_uri=task.workspace.canonical_uri,
            base_revision=bound.base_revision,
        )

    def claim_attempt(self, attempt_id: str, *, now: datetime | None = None, ttl: timedelta = timedelta(seconds=60)) -> WorkerAttempt:
        return self.queue.claim(attempt_id, now=now, ttl=ttl)

    def claim_next(self, *, now: datetime | None = None, ttl: timedelta = timedelta(seconds=60)) -> WorkerAttempt | None:
        return self.queue.claim_next(now=now, ttl=ttl)

    def requeue_attempt(self, attempt_id: str, *, checkpoint_store: CheckpointStore | None = None) -> WorkerAttempt:
        return self.queue.requeue(attempt_id, checkpoint_store=checkpoint_store)

    def ack_terminal(self, attempt_id: str, *, status: WorkerAttemptStatus | None = None) -> WorkerAttempt:
        return self.queue.ack_terminal(attempt_id, status=status)

    def heartbeat(self, attempt_id: str, token: str, *, now: datetime | None = None, ttl: timedelta = timedelta(seconds=60)) -> WorkerAttempt:
        attempt = self.store.attempts.get(attempt_id)
        if not attempt:
            raise DomainError("not_found", "worker attempt not found", 404)
        if not attempt.claim_token or not attempt.claim_expires_at:
            raise DomainError("invalid_claim", "attempt has no active claim")
        service = WorkerClaimService(ttl)
        renewed = service.heartbeat(AttemptRecord(id=attempt.id, worker_session_id=attempt.worker_session_id, status=attempt.status.value, claim=WorkerAttemptClaim(token=attempt.claim_token, claimed_at=attempt.claim_expires_at - ttl, expires_at=attempt.claim_expires_at)), token, now=now)
        updated = attempt.model_copy(update={"claim_expires_at": renewed.claim.expires_at, "version": attempt.version + 1})
        self.store.attempts[attempt.id] = updated
        self.store.commit({"type": "worker.heartbeat", "data": updated.model_dump(mode="json") | {"session_id": self.store.attempt_session.get(attempt.id)}})
        return updated

    def request_control(self, attempt_id: str, action: str) -> WorkerAttempt:
        """Persist a pause/cancel request and wake a local runner if present."""
        attempt = self.store.attempts.get(attempt_id)
        if attempt is None:
            raise DomainError("not_found", "worker attempt not found", 404)
        if action not in {"pause", "cancel"}:
            raise ValueError(f"unsupported execution control action: {action}")
        if attempt.status in {
            WorkerAttemptStatus.succeeded,
            WorkerAttemptStatus.partial,
            WorkerAttemptStatus.blocked,
            WorkerAttemptStatus.failed,
            WorkerAttemptStatus.cancelled,
        }:
            return attempt

        target = WorkerAttemptStatus.pause_requested if action == "pause" else WorkerAttemptStatus.cancel_requested
        if attempt.status is not WorkerAttemptStatus.running:
            # The existing state machine deliberately permits requests only
            # from a running attempt.  Queued attempts are left queued and
            # admission is prevented by the parent task control state.
            return attempt
        try:
            updated = transition_worker_attempt(attempt, target, attempt.version)
        except StateTransitionError as error:
            raise DomainError("invalid_transition", str(error)) from error
        self.store.attempts[attempt.id] = updated
        data = updated.model_dump(mode="json") | {"session_id": self.store.attempt_session.get(attempt.id)}
        self.store.commit({"type": f"worker.{target.value}", "data": data})
        # Store replay predates the request event names.  Keep a compatible
        # attempt snapshot event so a request survives process restart without
        # changing the domain state machine or storage package in this slice.
        self.store.commit({"type": "worker.checkpointed", "data": data})
        request_execution_control(self.store, attempt.id, action)
        return updated

    def recover_attempt(self, attempt_id: str, checkpoint_store: CheckpointStore) -> WorkerAttempt:
        return self.queue.recover(attempt_id, checkpoint_store)

    def validate_capacity(self, task: ParentTask, plan: WorkerPlan | None) -> None:
        if plan is None:
            return
        if len(plan.children) > 5:
            raise DomainError("worker_limit_exceeded", "a parent task can have at most 5 workers")
        session = self.store.sessions[task.controller_session_id]
        active = self.active_for_session(session.id)
        if active + len(plan.children) > session.max_active_workers:
            raise DomainError("worker_limit_exceeded", "controller session worker capacity is exhausted")

    def active_for_session(self, session_id: str) -> int:
        return sum(1 for attempt in self.store.attempts.values() if self.store.attempt_session.get(attempt.id) == session_id and attempt.status not in {
            WorkerAttemptStatus.succeeded, WorkerAttemptStatus.partial, WorkerAttemptStatus.blocked,
            WorkerAttemptStatus.failed, WorkerAttemptStatus.cancelled,
        })
