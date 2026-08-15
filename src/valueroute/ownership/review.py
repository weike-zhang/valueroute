"""Owner self-review lifecycle with fail-closed ownership checks."""

from __future__ import annotations

from typing import Any

from valueroute.domain.errors import DomainError
from valueroute.domain.models import OwnerSelfReview, ResourceRegion, ReviewStatus, new_id, now


class OwnerReviewService:
    """Create and transition reviews without widening an Owner's boundary."""

    def __init__(self, store: Any, ownership: Any) -> None:
        self.store = store
        self.ownership = ownership

    def submit(
        self,
        child_task_id: str,
        owner_agent_id: str,
        review_regions: list[ResourceRegion] | tuple[ResourceRegion, ...],
        evidence_ids: list[str] | tuple[str, ...],
        summary: str,
        *,
        expected_assignment_version: int,
        idem: tuple[str, str, str] | None = None,
        payload: Any | None = None,
    ) -> OwnerSelfReview:
        assignment = self.store.assignments.get(child_task_id)
        if assignment is None or assignment.status != "active":
            raise DomainError("owner_not_assigned", "child task has no active owner")
        if assignment.version != expected_assignment_version:
            raise DomainError("version_conflict", "owner assignment version has changed")
        if assignment.owner_agent_id != owner_agent_id:
            raise DomainError("owner_mismatch", "only the assigned owner can self-review")
        regions = tuple(review_regions)
        if not regions:
            raise DomainError("unknown_write_region", "self-review requires at least one region")
        for region in regions:
            self.ownership.assert_write_allowed(child_task_id, owner_agent_id, region)
        ids = tuple(dict.fromkeys(evidence_ids))
        if not ids:
            raise DomainError("evidence_required", "self-review requires evidence references")
        current = self.latest(child_task_id)
        if current is not None and current.status in {ReviewStatus.submitted, ReviewStatus.accepted}:
            raise DomainError("review_conflict", "child task already has an active self-review")
        review = OwnerSelfReview(
            id=new_id("review"),
            child_task_id=child_task_id,
            owner_agent_id=owner_agent_id,
            review_regions=regions,
            evidence_ids=ids,
            summary=summary,
        )
        self.store.reviews[review.id] = review
        self.store.commit({"type": "review.submitted", "data": review.model_dump(mode="json")}, key=idem, payload=payload)
        return review

    def latest(self, child_task_id: str) -> OwnerSelfReview | None:
        reviews = [review for review in self.store.reviews.values() if review.child_task_id == child_task_id]
        return max(reviews, key=lambda review: (review.updated_at, review.id), default=None)

    def transition(self, review: OwnerSelfReview, status: ReviewStatus | str, *, reason: str | None = None) -> OwnerSelfReview:
        status = ReviewStatus(status)
        if review.status is not ReviewStatus.submitted:
            raise DomainError("invalid_transition", "only a submitted self-review can be verified")
        if status not in {ReviewStatus.accepted, ReviewStatus.rejected}:
            raise DomainError("invalid_transition", "review transition must accept or reject")
        if status is ReviewStatus.rejected and not reason:
            raise DomainError("rejection_reason_required", "rejected review requires a reason")
        updated = review.model_copy(update={"status": status, "version": review.version + 1, "rejection_reason": reason, "updated_at": now()})
        self.store.reviews[review.id] = updated
        self.store.commit({"type": f"review.{status.value}", "data": updated.model_dump(mode="json")})
        return updated
