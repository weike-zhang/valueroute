"""Fail-closed parent-task completion contract.

This module is deliberately independent of persistence and API concerns.  It
only combines facts produced by child execution, ChangeSet integration, and
the evidence gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from valueroute.domain.models import TaskStatus
from valueroute.evidence.gate import EvidenceGateResult


@dataclass(frozen=True)
class ChildTaskResult:
    status: TaskStatus | str


@dataclass(frozen=True)
class ChangeSetResult:
    integrated: bool = False
    conflict: bool = False


@dataclass(frozen=True)
class ParentVerificationResult:
    status: TaskStatus
    can_complete: bool
    reasons: tuple[str, ...] = ()


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _status(value: Any) -> str:
    value = _value(value, "status", value)
    return getattr(value, "value", value)


class ParentVerification:
    """Calculate a parent's honest terminal outcome.

    ``completed`` is possible only when every child completed, every ChangeSet
    is integrated without conflict, and the evidence gate passed.  Unknown or
    missing integration facts are therefore rejected rather than inferred.
    """

    def evaluate(
        self,
        child_results: Iterable[ChildTaskResult | Mapping[str, Any] | Any],
        changesets: Iterable[ChangeSetResult | Mapping[str, Any] | Any],
        evidence_gate: EvidenceGateResult,
    ) -> ParentVerificationResult:
        children = list(child_results)
        changes = list(changesets)
        child_statuses = [_status(child) for child in children]
        reasons: list[str] = []

        if any(status in {TaskStatus.failed.value, "failure", "error"} for status in child_statuses):
            reasons.append("a child task failed")
            return ParentVerificationResult(TaskStatus.failed, False, tuple(reasons))
        if any(status == TaskStatus.cancelled.value for status in child_statuses):
            reasons.append("a child task was cancelled")
            return ParentVerificationResult(TaskStatus.blocked, False, tuple(reasons))
        if any(status == TaskStatus.blocked.value for status in child_statuses):
            reasons.append("a child task is blocked")
            return ParentVerificationResult(TaskStatus.blocked, False, tuple(reasons))

        conflicts = [
            change
            for change in changes
            if bool(_value(change, "conflict", False))
            or _status(change) in {"conflicted", "conflict"}
            or _value(change, "code") == "integration_conflict"
        ]
        not_integrated = [
            change
            for change in changes
            if _status(change) != "integrated" and not bool(_value(change, "integrated", False))
        ]
        if conflicts:
            reasons.append("a ChangeSet has an integration conflict")
            return ParentVerificationResult(TaskStatus.blocked, False, tuple(reasons))
        if not_integrated:
            reasons.append("a ChangeSet is not integrated")
            return ParentVerificationResult(TaskStatus.blocked, False, tuple(reasons))

        if not evidence_gate.passed:
            if evidence_gate.failed_required:
                reasons.append("required evidence failed")
                return ParentVerificationResult(TaskStatus.failed, False, tuple(reasons))
            if evidence_gate.unobserved_required:
                reasons.append("required evidence is unobserved")
            else:
                reasons.append("required evidence is missing")
            return ParentVerificationResult(TaskStatus.blocked, False, tuple(reasons))

        if any(status != TaskStatus.completed.value for status in child_statuses):
            reasons.append("not all child tasks completed")
            return ParentVerificationResult(TaskStatus.partial, False, tuple(reasons))
        return ParentVerificationResult(TaskStatus.completed, True)

    verify = evaluate


__all__ = ["ChangeSetResult", "ChildTaskResult", "ParentVerification", "ParentVerificationResult"]
