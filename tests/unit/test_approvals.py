from datetime import datetime, timedelta, timezone

import pytest

from valueroute.approvals import (
    Approval,
    ApprovalDecision,
    ApprovalDecisionConflict,
    ApprovalDecisionNotAllowed,
    ApprovalExpired,
    ApprovalService,
    ApprovalStatus,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def approval(**updates):
    values = {
        "id": "ap_1",
        "action_summary": "Deploy the production migration",
        "risk": "irreversible schema change",
        "expires_at": NOW + timedelta(minutes=5),
    }
    values.update(updates)
    return Approval(**values)


def test_pending_approval_can_be_approved_or_rejected():
    service = ApprovalService()

    approved = service.decide(approval(), ApprovalDecision.approve, reason="reviewed", now=NOW)
    rejected = service.decide(approval(id="ap_2"), ApprovalDecision.reject, now=NOW)

    assert approved.status is ApprovalStatus.approved
    assert approved.decision is ApprovalDecision.approve
    assert approved.reason == "reviewed"
    assert approved.decided_at == NOW
    assert rejected.status is ApprovalStatus.rejected
    assert rejected.decision is ApprovalDecision.reject


def test_decision_must_be_allowed():
    service = ApprovalService()
    restricted = approval(allowed_decisions=frozenset({ApprovalDecision.reject}))

    with pytest.raises(ApprovalDecisionNotAllowed, match="not allowed"):
        service.decide(restricted, ApprovalDecision.approve, now=NOW)


def test_same_decision_is_idempotent_and_opposing_decision_conflicts():
    service = ApprovalService()
    decided = service.decide(approval(), ApprovalDecision.approve, now=NOW)

    assert service.decide(decided, ApprovalDecision.approve, reason="ignored", now=NOW) == decided
    with pytest.raises(ApprovalDecisionConflict, match="already decided"):
        service.decide(decided, ApprovalDecision.reject, now=NOW)


def test_pending_approval_expires_and_cannot_be_decided():
    service = ApprovalService()
    pending = approval()

    assert service.expire(pending, now=pending.expires_at).status is ApprovalStatus.expired
    with pytest.raises(ApprovalExpired, match="expired"):
        service.decide(pending, ApprovalDecision.approve, now=pending.expires_at)
