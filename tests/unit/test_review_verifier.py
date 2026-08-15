from valueroute.domain.models import (
    Acceptance,
    EvidenceRecord,
    ObservationStatus,
    ParentTask,
    ResourceRegion,
    Workspace,
)
from valueroute.evidence.verifier import VerifierService
from valueroute.ownership.boundaries import ChildTaskBoundary, OwnershipBoundaryService
from valueroute.ownership.review import OwnerReviewService
from valueroute.storage.journal import LocalJournal
from valueroute.storage.store import Store


def test_owner_review_and_verification_are_scoped_and_replayed(tmp_path):
    region = ResourceRegion(resource_kind="file", resource_id="src/app.py", selector_type="whole_resource", selector_value="", base_revision="r1")
    child = ChildTaskBoundary(id="ct_review", parent_task_id="pt_review", objective="repair app", write_regions=(region,))
    ownership = OwnershipBoundaryService()
    ownership.register(child)
    assignment = ownership.assign(child.id, "owner-1", [region])

    journal = LocalJournal(tmp_path)
    store = Store(journal)
    store.children[child.id] = child
    store.assignments[child.id] = assignment
    task = ParentTask(
        id="pt_review",
        controller_session_id="cs",
        request_type="new_task",
        goal="repair",
        acceptance_contract=[Acceptance(id="a", description="pass")],
        data_classification="internal",
        workspace=Workspace(canonical_uri="workspace://project", base_revision="r1"),
        child_task_ids=[child.id],
        evidence=[EvidenceRecord(
            id="ev_review",
            requirement_id="a",
            evidence_type="test",
            observation_status=ObservationStatus.observed_pass,
            source="pytest",
            child_task_id=child.id,
            region=region,
        )],
    )
    store.tasks[task.id] = task
    reviews = OwnerReviewService(store, ownership)
    verifier = VerifierService(store, ownership, reviews)
    review = reviews.submit(child.id, "owner-1", [region], ["ev_review"], "checked the owned file", expected_assignment_version=1)
    result = verifier.verify(child.id, review.id, "owner-1", ["ev_review"], expected_review_version=review.version)

    assert result.status.value == "passed"
    assert store.reviews[review.id].status.value == "accepted"
    assert result.id in store.verifications
    journal.close()

    restarted = Store(LocalJournal(tmp_path))
    assert restarted.reviews[review.id].status.value == "accepted"
    assert restarted.verifications[result.id].status.value == "passed"
    restarted.journal.close()
