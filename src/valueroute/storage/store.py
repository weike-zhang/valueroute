from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Mapping

from valueroute.domain.models import (
    ControllerEpoch,
    ControllerSession,
    OwnerSelfReview,
    VerificationRecord,
    IntegrationAttempt,
    IntegrationAttemptStatus,
    ParentTask,
    WorkerAttempt,
    WorkerPlan,
    WriterLease,
)
from valueroute.observability.usage import UsageRecord
from valueroute.domain.models import WorkerAttemptStatus
from valueroute.storage.checkpoints import CheckpointStore
from valueroute.approvals import Approval
from valueroute.ownership.boundaries import ChildTaskBoundary, OwnerAssignment
from valueroute.storage.journal import JournalError, LocalJournal


class Store:
    def __init__(self, journal: LocalJournal, checkpoint_store: CheckpointStore | None = None):
        self.journal = journal
        self.sessions: dict[str, ControllerSession] = {}
        self.epochs: dict[str, ControllerEpoch] = {}
        self.tasks: dict[str, ParentTask] = {}
        self.plans: dict[str, WorkerPlan] = {}
        self.leases: dict[str, WriterLease] = {}
        self.attempts: dict[str, WorkerAttempt] = {}
        self.attempt_session: dict[str, str] = {}
        self.terminal_acks: set[str] = set()
        self.integration_attempts: dict[str, IntegrationAttempt] = {}
        self.usage: dict[str, list[UsageRecord]] = {}
        self.approvals: dict[str, Approval] = {}
        self.approval_task: dict[str, str] = {}
        self.children: dict[str, ChildTaskBoundary] = {}
        self.child_ref_ids: dict[tuple[str, str], str] = {}
        self.assignments: dict[str, OwnerAssignment] = {}
        self.reviews: dict[str, OwnerSelfReview] = {}
        self.verifications: dict[str, VerificationRecord] = {}
        self._replay()
        if checkpoint_store is not None:
            self.reclaim_attempts(checkpoint_store)

    def _replay(self) -> None:
        for event in self.journal.events():
            kind = event["type"]
            data = event["data"]
            if kind == "session.created": self.sessions[data["id"]] = ControllerSession.model_validate(data)
            elif kind == "session.epoch_registered":
                self.epochs[data["id"]] = ControllerEpoch.model_validate(data)
                session = self.sessions[data["controller_session_id"]]
                self.sessions[session.id] = session.model_copy(update={"active_controller_epoch_id": data["id"], "version": session.version + 1})
            elif kind == "task.created": self.tasks[data["id"]] = ParentTask.model_validate(data)
            elif kind == "task.updated": self.tasks[data["id"]] = ParentTask.model_validate(data)
            elif kind == "child_task.created":
                child = ChildTaskBoundary.model_validate(data)
                self.children[child.id] = child
            elif kind == "plan.committed":
                self.plans[data["id"]] = WorkerPlan.model_validate(data)
                task = self.tasks[data["parent_task_id"]]
                self.tasks[task.id] = task.model_copy(update={"plan_id": data["id"], "status": "planned", "version": task.version + 1})
            elif kind == "lease.acquired": self.leases[data["id"]] = WriterLease.model_validate(data)
            elif kind in {"lease.heartbeat", "lease.expired"}:
                self.leases[data["id"]] = WriterLease.model_validate(data)
            elif kind == "lease.released":
                lease = self.leases[data["id"]]
                self.leases[lease.id] = lease.model_copy(update={"status": "released", "version": lease.version + 1})
            elif kind == "worker.queued":
                self.attempts[data["id"]] = WorkerAttempt.model_validate({key: value for key, value in data.items() if key != "session_id"})
                if data.get("session_id"):
                    self.attempt_session[data["id"]] = data["session_id"]
            elif kind in {"worker.request_bound", "worker.claimed", "worker.heartbeat", "worker.recovered", "worker.requeued", "worker.blocked", "worker.started", "worker.stopped", "worker.cancel_failed", "worker.checkpointed"}:
                self.attempts[data["id"]] = WorkerAttempt.model_validate({key: value for key, value in data.items() if key != "session_id"})
                if data.get("session_id"):
                    self.attempt_session[data["id"]] = data["session_id"]
            elif kind == "worker.terminal_ack":
                self.attempts[data["id"]] = WorkerAttempt.model_validate({key: value for key, value in data.items() if key != "session_id"})
                if data.get("session_id"):
                    self.attempt_session[data["id"]] = data["session_id"]
                self.terminal_acks.add(data["id"])
            elif kind in {"integration.queued", "integration.started", "integration.requeued", "integration.completed", "integration.conflict", "integration.blocked"}:
                attempt = IntegrationAttempt.model_validate(data)
                self.integration_attempts[attempt.id] = attempt
            elif kind == "usage.recorded":
                usage = UsageRecord.model_validate(data)
                self.usage.setdefault(usage.task_id, []).append(usage)
            elif kind in {"approval.requested", "approval.approved", "approval.rejected", "approval.expired"}:
                approval = Approval.from_dict(data["approval"])
                self.approvals[approval.id] = approval
                if data.get("task_id"):
                    self.approval_task[approval.id] = data["task_id"]
            elif kind in {"ownership.owner_assigned", "ownership.owner_transferred", "ownership.owner_released"}:
                assignment = OwnerAssignment.model_validate(data)
                self.assignments[assignment.child_task_id] = assignment
            elif kind in {"review.submitted", "review.accepted", "review.rejected"}:
                review = OwnerSelfReview.model_validate(data)
                self.reviews[review.id] = review
            elif kind == "verification.recorded":
                verification = VerificationRecord.model_validate(data)
                self.verifications[verification.id] = verification

    def commit_frame(
        self,
        events: Iterable[dict[str, Any]],
        *,
        expected_versions: Mapping[str, int] | None = None,
        key: tuple[str, str, str] | None = None,
        payload: Any = None,
    ) -> None:
        """Persist one business operation as one journal commit frame.

        Callers update their in-memory projection only after this method
        returns. The journal validates and fsyncs the complete frame, including
        expected versions and an idempotency result, before acknowledging it.
        """
        events = list(events)
        idem = None
        if key:
            request_hash = self.journal.request_hash(payload)
            response = {"event": events[0]} if len(events) == 1 else {"events": events}
            idem = (key, request_hash, response)
        self.journal.append_frame(events, expected_versions=expected_versions, idempotency=idem)

    def commit(self, event: dict[str, Any], *, key: tuple[str, str, str] | None = None, payload: Any = None) -> None:
        """Compatibility wrapper for a single-event commit frame."""
        self.commit_frame([event], key=key, payload=payload)

    def _aggregate_versions(self) -> dict[str, int]:
        aggregates: dict[str, int] = {}
        collections = (
            ("controller_session", self.sessions),
            ("controller_epoch", self.epochs),
            ("parent_task", self.tasks),
            ("worker_plan", self.plans),
            ("writer_lease", self.leases),
            ("worker_attempt", self.attempts),
            ("integration_attempt", self.integration_attempts),
            ("child_task", self.children),
            ("owner_assignment", self.assignments),
            ("owner_self_review", self.reviews),
            ("verification", self.verifications),
        )
        for kind, values in collections:
            for identifier, value in values.items():
                version = getattr(value, "version", None)
                if isinstance(version, int):
                    aggregates[f"{kind}:{identifier}"] = version
        return aggregates

    def rebuild(self) -> dict[str, Any]:
        """Report the current replay projection without mutating durable state."""
        return {"sequence": self.journal.sequence, "aggregate_versions": self._aggregate_versions()}

    def snapshot(self) -> dict[str, Any]:
        return self.journal.snapshot(aggregate_versions=self._aggregate_versions())

    def compact(self) -> dict[str, Any]:
        return self.journal.compact(aggregate_versions=self._aggregate_versions())

    def check_idempotency(self, key: tuple[str, str, str] | None, payload: Any) -> dict[str, Any] | None:
        if not key: return None
        return self.journal.idempotent_result(key, self.journal.request_hash(payload))

    def record_integration_attempt(self, attempt: IntegrationAttempt, event_type: str) -> IntegrationAttempt:
        """Commit an IntegrationAttempt transition and update the derived index."""
        if event_type not in {"integration.queued", "integration.started", "integration.requeued", "integration.completed", "integration.conflict", "integration.blocked"}:
            raise ValueError("invalid_integration_event")
        self.commit({"type": event_type, "data": attempt.model_dump(mode="json")})
        self.integration_attempts[attempt.id] = attempt
        return attempt

    def latest_integration_attempt(self, client_ref: str, parent_task_id: str | None = None) -> IntegrationAttempt | None:
        """Return the latest journaled attempt for one ordered item."""
        found = [
            attempt
            for attempt in self.integration_attempts.values()
            if attempt.client_ref == client_ref and (parent_task_id is None or attempt.parent_task_id == parent_task_id)
        ]
        return found[-1] if found else None

    def integration_attempts_for_order(
        self,
        integration_order: list[str] | tuple[str, ...],
        parent_task_id: str | None = None,
    ) -> list[IntegrationAttempt]:
        """Recover the latest attempt for each client reference in plan order."""
        recovered: list[IntegrationAttempt] = []
        for client_ref in integration_order:
            attempt = self.latest_integration_attempt(client_ref, parent_task_id)
            if attempt is not None:
                recovered.append(attempt)
        return recovered

    def recover_integration_results(
        self,
        integration_order: list[str] | tuple[str, ...],
        parent_task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return journal-backed integration results in the supplied order."""
        return [self.integration_result(attempt) for attempt in self.integration_attempts_for_order(integration_order, parent_task_id)]

    @classmethod
    def integration_result(cls, attempt: IntegrationAttempt) -> dict[str, Any]:
        return cls._integration_result(attempt)

    @staticmethod
    def _integration_result(attempt: IntegrationAttempt) -> dict[str, Any]:
        result: dict[str, Any] = {
            "client_ref": attempt.client_ref,
            "status": "blocked" if attempt.status in {IntegrationAttemptStatus.conflicted, IntegrationAttemptStatus.rejected} else attempt.status.value,
        }
        if attempt.code is not None:
            result["code"] = attempt.code
        if attempt.message is not None:
            result["message"] = attempt.message
        if attempt.owner_id is not None:
            result["owner_id"] = attempt.owner_id
        if attempt.revision is not None:
            result["revision"] = attempt.revision
        return result

    def require_version(self, actual: int, expected: int) -> None:
        if actual != expected:
            raise ValueError("version_conflict")

    def record_usage(self, usage: UsageRecord) -> UsageRecord:
        self.usage.setdefault(usage.task_id, []).append(usage)
        self.commit({"type": "usage.recorded", "data": usage.model_dump(mode="json")})
        return usage

    def reclaim_attempts(self, checkpoint_store: CheckpointStore) -> None:
        safe_ids = {checkpoint.id for checkpoint in checkpoint_store.list_valid() if checkpoint.safe_to_resume}
        for attempt in list(self.attempts.values()):
            if attempt.status not in {WorkerAttemptStatus.claimed, WorkerAttemptStatus.running}:
                continue
            # A recovery attempt may retain the checkpoint from the current
            # try as well as the explicitly selected safe recovery point. On
            # restart, the recovery lineage is the authoritative resume point.
            checkpoint_id = attempt.recovery_checkpoint_id or attempt.checkpoint_id
            target = WorkerAttemptStatus.queued if checkpoint_id in safe_ids else WorkerAttemptStatus.blocked
            updated = attempt.model_copy(update={"status": target, "claim_token": None, "claim_expires_at": None})
            self.attempts[attempt.id] = updated
            event_type = "worker.recovered" if target == WorkerAttemptStatus.queued else "worker.blocked"
            self.commit({"type": event_type, "data": updated.model_dump(mode="json") | {"session_id": self.attempt_session.get(attempt.id)}})
