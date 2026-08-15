"""Verifier lifecycle for Owner self-reviews."""

from __future__ import annotations

from typing import Any

from valueroute.domain.errors import DomainError
from valueroute.domain.models import (
    EvidenceRecord,
    ObservationStatus,
    ResourceRegion,
    ReviewStatus,
    VerificationRecord,
    VerificationStatus,
    new_id,
)


class VerifierService:
    """Verify only persisted, scoped, observed evidence for the assigned Owner."""

    def __init__(self, store: Any, ownership: Any, reviews: Any) -> None:
        self.store = store
        self.ownership = ownership
        self.reviews = reviews

    def verify(
        self,
        child_task_id: str,
        review_id: str,
        verifier_agent_id: str,
        evidence_ids: list[str] | tuple[str, ...],
        *,
        expected_review_version: int,
        idem: tuple[str, str, str] | None = None,
        payload: Any | None = None,
    ) -> VerificationRecord:
        review = self.store.reviews.get(review_id)
        if review is None or review.child_task_id != child_task_id:
            raise DomainError("not_found", "self-review not found", 404)
        if review.version != expected_review_version:
            raise DomainError("version_conflict", "self-review version has changed")
        assignment = self.store.assignments.get(child_task_id)
        if assignment is None or assignment.status != "active":
            raise DomainError("owner_not_assigned", "child task has no active owner")
        if assignment.owner_agent_id != verifier_agent_id or review.owner_agent_id != verifier_agent_id:
            raise DomainError("owner_mismatch", "Verifier must be the Owner of the reviewed region")
        for region in review.review_regions:
            self.ownership.assert_write_allowed(child_task_id, verifier_agent_id, region)
        ids = tuple(dict.fromkeys(evidence_ids)) or review.evidence_ids
        records = self._evidence_for_child(child_task_id, ids)
        reasons: list[str] = []
        if review.status.value != "submitted":
            reasons.append("review_not_submitted")
        if not records:
            reasons.append("evidence_missing")
        if len(records) != len(ids):
            reasons.append("evidence_reference_missing")
        if any(record.observation_status is not ObservationStatus.observed_pass for record in records):
            reasons.append("evidence_not_observed_pass")
        if any(record.region is None for record in records):
            reasons.append("evidence_region_missing")
        if any(record.region is not None and not any(self._covers(review_region, record.region) for review_region in review.review_regions) for record in records):
            reasons.append("evidence_region_out_of_scope")
        status = VerificationStatus.passed if not reasons else VerificationStatus.blocked
        verification = VerificationRecord(
            id=new_id("verification"),
            child_task_id=child_task_id,
            review_id=review.id,
            verifier_agent_id=verifier_agent_id,
            evidence_ids=ids,
            status=status,
            reasons=tuple(dict.fromkeys(reasons)),
            checked_regions=review.review_regions,
        )
        self.store.verifications[verification.id] = verification
        self.store.commit({"type": "verification.recorded", "data": verification.model_dump(mode="json")}, key=idem, payload=payload)
        if status is VerificationStatus.passed:
            self.reviews.transition(review, ReviewStatus.accepted)
        return verification

    def _evidence_for_child(self, child_task_id: str, ids: tuple[str, ...]) -> list[EvidenceRecord]:
        task = next((task for task in self.store.tasks.values() if child_task_id in task.child_task_ids), None)
        if task is None:
            return []
        by_id = {record.id: record for record in task.evidence}
        records = [by_id[item] for item in ids if item in by_id]
        return [record for record in records if record.child_task_id == child_task_id]

    @staticmethod
    def _covers(allowed: ResourceRegion, requested: ResourceRegion) -> bool:
        if (allowed.resource_kind, allowed.resource_id, allowed.base_revision) != (requested.resource_kind, requested.resource_id, requested.base_revision):
            return False
        if allowed.selector_type == "whole_resource":
            return True
        if allowed.selector_type == requested.selector_type == "path_prefix":
            prefix = str(allowed.selector_value).rstrip("/")
            path = str(requested.selector_value).rstrip("/")
            return path == prefix or path.startswith(prefix + "/")
        return allowed.selector_type == requested.selector_type and allowed.selector_value == requested.selector_value
