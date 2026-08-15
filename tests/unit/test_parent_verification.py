from valueroute.domain.models import Acceptance, EvidenceRecord, ObservationStatus, TaskStatus
from valueroute.evidence import EvidenceGate
from valueroute.integration.parent_verification import ChangeSetResult, ChildTaskResult, ParentVerification


def gate(status=ObservationStatus.observed_pass):
    return EvidenceGate().evaluate(
        [Acceptance(id="a", description="works")],
        [EvidenceRecord(id="e", requirement_id="a", evidence_type="test", observation_status=status, source="pytest")],
    )


def test_completed_requires_all_child_changesets_and_observed_evidence():
    result = ParentVerification().evaluate([ChildTaskResult(TaskStatus.completed)], [ChangeSetResult(integrated=True)], gate())
    assert result == result.__class__(TaskStatus.completed, True)


def test_unobserved_required_evidence_blocks_completion():
    result = ParentVerification().evaluate([ChildTaskResult(TaskStatus.completed)], [ChangeSetResult(integrated=True)], gate(ObservationStatus.unobserved))
    assert result.status is TaskStatus.blocked
    assert not result.can_complete


def test_conflict_or_unintegrated_changeset_blocks_completion():
    verifier = ParentVerification()
    for change in (ChangeSetResult(conflict=True), ChangeSetResult()):
        result = verifier.evaluate([ChildTaskResult(TaskStatus.completed)], [change], gate())
        assert result.status is TaskStatus.blocked
        assert not result.can_complete


def test_child_failure_is_failed_and_incomplete_child_is_partial():
    verifier = ParentVerification()
    assert verifier.evaluate([ChildTaskResult(TaskStatus.failed)], [], gate()).status is TaskStatus.failed
    assert verifier.evaluate([ChildTaskResult(TaskStatus.partial)], [], gate()).status is TaskStatus.partial
