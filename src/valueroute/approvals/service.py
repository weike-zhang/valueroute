from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from valueroute.approvals.types import Approval, ApprovalDecision, ApprovalStatus


class ApprovalDecisionNotAllowed(ValueError):
    """Raised when the requested decision was not offered by the approval."""


class ApprovalDecisionConflict(ValueError):
    """Raised when a terminal approval receives the opposing decision."""


class ApprovalExpired(ValueError):
    """Raised when a decision arrives after the approval deadline."""


class ApprovalVersionConflict(ValueError):
    """Raised when a write was based on a stale Approval version."""


class ApprovalService:
    """Pure transition rules for Approval records."""

    def expire(self, approval: Approval, *, now: datetime | None = None) -> Approval:
        moment = _utc(now or datetime.now(timezone.utc))
        if approval.status is ApprovalStatus.pending and _utc(approval.expires_at) <= moment:
            return replace(approval, status=ApprovalStatus.expired, version=approval.version + 1)
        return approval

    def decide(
        self,
        approval: Approval,
        decision: ApprovalDecision,
        *,
        reason: str | None = None,
        expected_version: int | None = None,
        now: datetime | None = None,
    ) -> Approval:
        if expected_version is not None and expected_version != approval.version:
            raise ApprovalVersionConflict("approval version has changed")
        if decision not in approval.allowed_decisions:
            raise ApprovalDecisionNotAllowed(f"decision {decision.value} is not allowed")

        current = self.expire(approval, now=now)
        if current.status is ApprovalStatus.expired:
            raise ApprovalExpired("approval has expired")
        if current.status is ApprovalStatus.approved or current.status is ApprovalStatus.rejected:
            if current.decision is decision:
                return current
            raise ApprovalDecisionConflict(
                f"approval already decided as {current.decision.value}"
            )

        moment = _utc(now or datetime.now(timezone.utc))
        status = (
            ApprovalStatus.approved
            if decision is ApprovalDecision.approve
            else ApprovalStatus.rejected
        )
        return replace(
            current,
            status=status,
            decision=decision,
            reason=reason,
            decided_at=moment,
            version=current.version + 1,
        )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("approval timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)
