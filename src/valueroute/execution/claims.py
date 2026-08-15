"""Pure claim/recovery rules for WorkerAttempt execution.

The service deliberately knows nothing about the project's Store or domain
models.  Callers provide small records and persist the returned values.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from secrets import token_urlsafe
from typing import Iterable, Protocol


TERMINAL = frozenset({"succeeded", "partial", "blocked", "failed", "cancelled"})


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class WorkerAttemptClaim:
    token: str
    claimed_at: datetime
    expires_at: datetime

    @property
    def expired(self) -> bool:
        return self.expires_at <= datetime.now(timezone.utc)


@dataclass(frozen=True)
class AttemptRecord:
    id: str
    worker_session_id: str
    status: str = "queued"
    claim: WorkerAttemptClaim | None = None
    checkpoint_id: str | None = None
    checkpoint_safe_to_resume: bool = False
    resumed_from_attempt_id: str | None = None
    recovery_checkpoint_id: str | None = None


class Checkpoint(Protocol):
    id: str
    safe_to_resume: bool


@dataclass(frozen=True)
class RecoveryDecision:
    attempt_id: str
    action: str  # "requeue" or "block"
    checkpoint_id: str | None = None


class WorkerClaimService:
    """State transitions that can be tested without storage or API objects."""

    def __init__(self, ttl: timedelta):
        if ttl <= timedelta(0):
            raise ValueError("claim TTL must be positive")
        self.ttl = ttl

    def claim(self, attempt: AttemptRecord, *, now: datetime | None = None) -> AttemptRecord:
        if attempt.status != "queued":
            raise ValueError("only queued attempts can be claimed")
        moment = _utc(now or datetime.now(timezone.utc))
        claim = WorkerAttemptClaim(token=token_urlsafe(32), claimed_at=moment, expires_at=moment + self.ttl)
        return replace(attempt, status="claimed", claim=claim)

    def heartbeat(self, attempt: AttemptRecord, token: str, *, now: datetime | None = None) -> AttemptRecord:
        moment = _utc(now or datetime.now(timezone.utc))
        claim = attempt.claim
        if attempt.status not in {"claimed", "running"} or claim is None or claim.token != token or claim.expires_at <= moment:
            raise ValueError("invalid or expired claim")
        return replace(attempt, claim=replace(claim, expires_at=moment + self.ttl))

    def create_recovery_attempt(
        self,
        attempt: AttemptRecord,
        *,
        checkpoint_id: str,
        attempt_id: str,
    ) -> AttemptRecord:
        """Create a new queued try; the prior attempt remains immutable/history."""
        if attempt.status != "blocked" and attempt.status not in TERMINAL:
            raise ValueError("only a terminated attempt can be resumed")
        if not checkpoint_id:
            raise ValueError("a recovery checkpoint is required")
        return AttemptRecord(
            id=attempt_id,
            worker_session_id=attempt.worker_session_id,
            checkpoint_id=checkpoint_id,
            recovery_checkpoint_id=checkpoint_id,
            resumed_from_attempt_id=attempt.id,
        )

    def reclaim_on_startup(
        self,
        attempts: Iterable[AttemptRecord],
        checkpoints: Iterable[Checkpoint] = (),
    ) -> tuple[list[AttemptRecord], list[RecoveryDecision]]:
        safe = {checkpoint.id for checkpoint in checkpoints if checkpoint.safe_to_resume}
        updated: list[AttemptRecord] = []
        decisions: list[RecoveryDecision] = []
        for attempt in attempts:
            if attempt.status in TERMINAL or attempt.claim is None:
                updated.append(attempt)
                continue
            checkpoint_id = attempt.recovery_checkpoint_id or attempt.checkpoint_id
            if checkpoint_id in safe or (checkpoint_id is not None and attempt.checkpoint_safe_to_resume):
                updated.append(replace(attempt, status="queued", claim=None))
                decisions.append(RecoveryDecision(attempt.id, "requeue", checkpoint_id))
            else:
                updated.append(replace(attempt, status="blocked", claim=None))
                decisions.append(RecoveryDecision(attempt.id, "block"))
        return updated, decisions


__all__ = ["AttemptRecord", "Checkpoint", "RecoveryDecision", "WorkerAttemptClaim", "WorkerClaimService"]
