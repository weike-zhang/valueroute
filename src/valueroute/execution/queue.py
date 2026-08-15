"""Local, journal-backed execution queue.

The queue is intentionally only an in-memory view over ``Store.attempts``.
Every mutation is committed to the journal before the local index is changed;
after a restart the queued view is rebuilt from the Store replay.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from valueroute.domain.errors import DomainError
from valueroute.domain.models import WorkerAttempt, WorkerAttemptStatus, new_id
from valueroute.execution.claims import AttemptRecord, WorkerClaimService
from valueroute.storage.checkpoints import CheckpointStore
from valueroute.storage.store import Store


TERMINAL_STATUSES = frozenset(
    {
        WorkerAttemptStatus.succeeded,
        WorkerAttemptStatus.partial,
        WorkerAttemptStatus.blocked,
        WorkerAttemptStatus.failed,
        WorkerAttemptStatus.cancelled,
    }
)


class LocalExecutionQueue:
    """A local FIFO view whose durable source of truth is the Store journal."""

    def __init__(self, store: Store):
        self.store = store

    def enqueue(self, attempt: WorkerAttempt, *, session_id: str | None = None) -> WorkerAttempt:
        """Durably admit one queued attempt, idempotently for the same record."""
        existing = self.store.attempts.get(attempt.id)
        if existing is not None:
            if existing != attempt:
                raise DomainError("already_exists", "worker attempt already exists")
            return existing
        if attempt.status is not WorkerAttemptStatus.queued:
            raise DomainError("invalid_transition", "only queued attempts can be enqueued")

        session = session_id or self.store.attempt_session.get(attempt.id)
        data = attempt.model_dump(mode="json") | {"session_id": session}
        self.store.commit({"type": "worker.queued", "data": data})
        self.store.attempts[attempt.id] = attempt
        if session:
            self.store.attempt_session[attempt.id] = session
        return attempt

    def queued(self) -> list[WorkerAttempt]:
        """Return the current FIFO view rebuilt from Store state."""
        return [
            attempt
            for attempt in self.store.attempts.values()
            if attempt.status is WorkerAttemptStatus.queued
        ]

    def claim_next(
        self,
        *,
        now: datetime | None = None,
        ttl: timedelta = timedelta(seconds=60),
    ) -> WorkerAttempt | None:
        """Claim the next durable queued attempt, or return ``None``."""
        queued = self.queued()
        if not queued:
            return None
        return self.claim(queued[0].id, now=now, ttl=ttl)

    def claim(
        self,
        attempt_id: str,
        *,
        now: datetime | None = None,
        ttl: timedelta = timedelta(seconds=60),
    ) -> WorkerAttempt:
        """Persist a claim token and return the claimed attempt."""
        attempt = self.store.attempts.get(attempt_id)
        if attempt is None:
            raise DomainError("not_found", "worker attempt not found", 404)
        service = WorkerClaimService(ttl)
        try:
            claimed = service.claim(
                AttemptRecord(
                    id=attempt.id,
                    worker_session_id=attempt.worker_session_id,
                    status=attempt.status.value,
                    checkpoint_id=attempt.checkpoint_id,
                    checkpoint_safe_to_resume=attempt.checkpoint_safe_to_resume,
                ),
                now=now,
            )
        except ValueError as error:
            raise DomainError("invalid_claim", str(error)) from error
        updated = attempt.model_copy(
            update={
                "status": WorkerAttemptStatus.claimed,
                "claim_token": claimed.claim.token,
                "claim_expires_at": claimed.claim.expires_at,
                "version": attempt.version + 1,
            }
        )
        self.store.commit(
            {
                "type": "worker.claimed",
                "data": updated.model_dump(mode="json")
                | {"session_id": self.store.attempt_session.get(attempt.id)},
            }
        )
        self.store.attempts[attempt.id] = updated
        return updated

    def requeue(
        self,
        attempt_id: str,
        *,
        checkpoint_store: CheckpointStore | None = None,
    ) -> WorkerAttempt:
        """Return a safely resumable non-terminal attempt to the queue."""
        attempt = self.store.attempts.get(attempt_id)
        if attempt is None:
            raise DomainError("not_found", "worker attempt not found", 404)
        if attempt.status is WorkerAttemptStatus.queued:
            return attempt
        if attempt.status in TERMINAL_STATUSES:
            raise DomainError("invalid_transition", "terminal attempt cannot be requeued")
        if attempt.status not in {
            WorkerAttemptStatus.claimed,
            WorkerAttemptStatus.running,
            WorkerAttemptStatus.paused,
        }:
            raise DomainError("invalid_transition", f"attempt cannot be requeued from {attempt.status.value}")

        checkpoint_id = attempt.recovery_checkpoint_id or attempt.checkpoint_id
        safe = attempt.checkpoint_safe_to_resume
        if checkpoint_store is not None:
            safe = False
            if checkpoint_id:
                try:
                    safe = checkpoint_store.load(checkpoint_id).safe_to_resume
                except Exception:
                    safe = False
        if not safe:
            raise DomainError("not_resumable", "attempt has no safe recovery checkpoint")

        updated = attempt.model_copy(
            update={
                "status": WorkerAttemptStatus.queued,
                "claim_token": None,
                "claim_expires_at": None,
                "version": attempt.version + 1,
            }
        )
        self.store.commit(
            {
                "type": "worker.requeued",
                "data": updated.model_dump(mode="json")
                | {"session_id": self.store.attempt_session.get(attempt.id)},
            }
        )
        self.store.attempts[attempt.id] = updated
        return updated

    def recover(
        self,
        attempt_id: str,
        checkpoint_store: CheckpointStore,
    ) -> WorkerAttempt:
        """Create and durably enqueue a new attempt from a safe checkpoint."""
        previous = self.store.attempts.get(attempt_id)
        if previous is None:
            raise DomainError("not_found", "worker attempt not found", 404)
        checkpoint_id = previous.recovery_checkpoint_id or previous.checkpoint_id
        if not checkpoint_id:
            raise DomainError("not_resumable", "attempt has no recovery checkpoint")
        try:
            checkpoint = checkpoint_store.load(checkpoint_id)
        except Exception as error:
            raise DomainError("not_resumable", "recovery checkpoint is unavailable") from error
        if not checkpoint.safe_to_resume or not previous.checkpoint_safe_to_resume:
            raise DomainError("not_resumable", "recovery checkpoint is not safe to resume")

        recovery = WorkerAttempt(
            id=new_id("wa"),
            worker_session_id=previous.worker_session_id,
            child_task_id=previous.child_task_id,
            workspace=previous.workspace,
            resumed_from_attempt_id=previous.id,
            checkpoint_id=checkpoint.id,
            recovery_checkpoint_id=checkpoint.id,
            checkpoint_safe_to_resume=True,
        )
        return self.enqueue(recovery, session_id=self.store.attempt_session.get(previous.id))

    def ack_terminal(
        self,
        attempt_id: str,
        *,
        status: WorkerAttemptStatus | None = None,
    ) -> WorkerAttempt:
        """Persist an idempotent acknowledgement for a terminal attempt."""
        attempt = self.store.attempts.get(attempt_id)
        if attempt is None:
            raise DomainError("not_found", "worker attempt not found", 404)
        if attempt.status not in TERMINAL_STATUSES:
            raise DomainError("invalid_transition", "only terminal attempts can be acknowledged")
        if status is not None and status is not attempt.status:
            raise DomainError("invalid_transition", "terminal acknowledgement status does not match attempt")
        if attempt_id in self.store.terminal_acks:
            return attempt

        self.store.commit(
            {
                "type": "worker.terminal_ack",
                "data": attempt.model_dump(mode="json")
                | {"session_id": self.store.attempt_session.get(attempt.id)},
            }
        )
        self.store.terminal_acks.add(attempt_id)
        return attempt


ExecutionQueue = LocalExecutionQueue

__all__ = ["ExecutionQueue", "LocalExecutionQueue", "TERMINAL_STATUSES"]
