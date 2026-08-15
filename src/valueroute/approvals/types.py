from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class ApprovalStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    expired = "expired"


class ApprovalDecision(str, Enum):
    approve = "approve"
    reject = "reject"


@dataclass(frozen=True)
class Approval:
    """A high-risk action awaiting a human decision.

    This is intentionally persistence-agnostic: callers persist the returned
    value atomically with their own event and idempotency record.
    """

    id: str
    action_summary: str
    risk: str
    expires_at: datetime
    version: int = 1
    allowed_decisions: frozenset[ApprovalDecision] = field(
        default_factory=lambda: frozenset({ApprovalDecision.approve, ApprovalDecision.reject})
    )
    status: ApprovalStatus = ApprovalStatus.pending
    decision: ApprovalDecision | None = None
    reason: str | None = None
    decided_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "version": self.version, "action_summary": self.action_summary, "risk": self.risk,
            "expires_at": self.expires_at.isoformat(), "allowed_decisions": sorted(item.value for item in self.allowed_decisions),
            "status": self.status.value, "decision": self.decision.value if self.decision else None,
            "reason": self.reason, "decided_at": self.decided_at.isoformat() if self.decided_at else None,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "Approval":
        return cls(
            id=value["id"], version=int(value.get("version", 1)), action_summary=value["action_summary"], risk=value["risk"],
            expires_at=datetime.fromisoformat(value["expires_at"]),
            allowed_decisions=frozenset(ApprovalDecision(item) for item in value.get("allowed_decisions", ["approve", "reject"])),
            status=ApprovalStatus(value.get("status", "pending")),
            decision=ApprovalDecision(value["decision"]) if value.get("decision") else None,
            reason=value.get("reason"), decided_at=datetime.fromisoformat(value["decided_at"]) if value.get("decided_at") else None,
        )
