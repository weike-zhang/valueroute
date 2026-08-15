import pytest

from valueroute.domain.models import Acceptance, EvidenceRecord, ObservationStatus
from valueroute.evidence import EvidenceGate
from valueroute.observability import CostStatus, UsageRecord


def test_evidence_gate_requires_observation_for_required_acceptance():
    acceptance = [Acceptance(id="a1", description="works")]
    result = EvidenceGate().evaluate(acceptance, [EvidenceRecord(id="e1", requirement_id="a1", evidence_type="live_check", observation_status=ObservationStatus.unobserved, source="browser")])
    assert not result.can_complete
    assert result.unobserved_required == ("a1",)


def test_evidence_gate_uses_latest_observation():
    acceptance = [Acceptance(id="a1", description="works")]
    old = EvidenceRecord(id="e1", requirement_id="a1", evidence_type="test", observation_status=ObservationStatus.unobserved, source="pytest")
    new = old.model_copy(update={"id": "e2", "observation_status": ObservationStatus.observed_pass})
    assert EvidenceGate().evaluate(acceptance, [old, new]).can_complete


def test_usage_unknown_cost_is_not_zero():
    usage = UsageRecord(id="u1", task_id="t1", provider_id="p1", model_id="m1", latency_ms=12)
    assert usage.cost_status is CostStatus.unknown
    assert usage.cost_usd is None
    with pytest.raises(ValueError, match="unknown cost"):
        UsageRecord(id="u2", task_id="t1", provider_id="p1", model_id="m1", latency_ms=12, cost_usd=0)
