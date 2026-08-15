import pytest

from valueroute.routing.advisory import AdvisoryEngine
from valueroute.routing.boundary import classify_boundary
from valueroute.routing.models import (
    RoutingPermissions,
    RoutingRequestEnvelope,
    RoutingResourceSummary,
)
from valueroute.routing.profiler import Profiler
from valueroute.routing.service import RoutingService
from valueroute.routing.shadow import ShadowLedger, envelope_hash
from valueroute.storage.journal import LocalJournal
from valueroute.storage.store import Store


def make_envelope(**overrides):
    base = {
        "tenant_id": "tenant_a",
        "host_session_id": "sess_1",
        "user_text": "fix the failing test in valueroute/domain",
    }
    base.update(overrides)
    return RoutingRequestEnvelope.model_validate(base)


class TestBoundary:
    def test_host_declared_wins_with_full_confidence(self):
        envelope = make_envelope(host_declared_request_type="clarification", user_text="fix the tests")
        decision = classify_boundary(envelope)
        assert decision.request_type == "clarification"
        assert decision.confidence == 1.0
        assert decision.method == "host_declared"

    def test_amendment_markers_select_material_amendment(self):
        decision = classify_boundary(make_envelope(user_text="增加一个新接口并调整范围"))
        assert decision.request_type == "material_amendment"

    def test_control_markers_select_control(self):
        decision = classify_boundary(make_envelope(user_text="暂停并取消当前任务"))
        assert decision.request_type == "control"

    def test_clarification_markers_select_clarification(self):
        decision = classify_boundary(make_envelope(user_text="为什么这个接口返回 500？"))
        assert decision.request_type == "clarification"

    def test_plain_text_falls_back_to_low_confidence_new_task(self):
        decision = classify_boundary(make_envelope(user_text="write a brand new module"))
        assert decision.request_type == "new_task"
        assert decision.confidence < 1.0
        assert decision.method == "rule_based"


class TestProfiler:
    def test_graph_is_read_only_and_never_suggests_writes(self):
        envelope = make_envelope(
            resource_summary=RoutingResourceSummary(
                canonical_uri="file:///repos/vr/src/valueroute",
                base_revision="abc123",
                referenced_paths=["src/valueroute/domain"],
            )
        )
        graph = Profiler().profile(envelope)
        assert graph.generated_by == "profiler"
        assert graph.has_write_suggestion is False
        assert any(node.id == "req_goal" for node in graph.requirements)
        assert any(node.id == "req_resources" for node in graph.requirements)

    def test_missing_resource_summary_produces_evidence_gap(self):
        graph = Profiler().profile(make_envelope())
        assert any(gap.id == "gap_resource_summary" for gap in graph.evidence_gaps)

    def test_empty_resource_summary_flags_region_independence_gap(self):
        envelope = make_envelope(
            resource_summary=RoutingResourceSummary(
                canonical_uri="file:///repos/vr",
                base_revision="abc123",
            )
        )
        graph = Profiler().profile(envelope)
        assert any(gap.id == "gap_regions" for gap in graph.evidence_gaps)


class TestAdvisoryEngine:
    def test_control_request_is_never_delegated(self):
        envelope = make_envelope(host_declared_request_type="control", user_text="取消任务")
        boundary = classify_boundary(envelope)
        graph = Profiler().profile(envelope)
        advice = AdvisoryEngine().advise(envelope, boundary, graph)
        assert advice.rejected is True
        assert any("control" in reason for reason in advice.rejection_reasons)
        workers = [c for c in advice.candidates if c.mode == "workers"]
        assert all(c.worker_count == 0 for c in workers)

    def test_worker_candidate_fails_closed_without_write_regions(self):
        envelope = make_envelope(
            resource_summary=RoutingResourceSummary(
                canonical_uri="file:///repos/vr",
                base_revision="abc123",
                referenced_paths=["src/valueroute"],
            )
        )
        boundary = classify_boundary(envelope)
        graph = Profiler().profile(envelope)
        advice = AdvisoryEngine().advise(envelope, boundary, graph)
        assert advice.rejected is True
        assert any("write regions" in reason for reason in advice.rejection_reasons)
        assert all(c.worker_count == 0 for c in advice.candidates if c.mode == "workers")

    def test_worker_candidate_is_proposed_when_regions_and_summary_exist(self):
        envelope = make_envelope(
            permissions=RoutingPermissions(
                requested_write_regions=[
                    {"resource_kind": "file", "resource_id": "src/a.py", "selector_type": "whole_resource", "selector_value": "*", "base_revision": "abc123"},
                    {"resource_kind": "file", "resource_id": "src/b.py", "selector_type": "whole_resource", "selector_value": "*", "base_revision": "abc123"},
                ]
            ),
            resource_summary=RoutingResourceSummary(
                canonical_uri="file:///repos/vr",
                base_revision="abc123",
                referenced_paths=["src/a.py", "src/b.py"],
            ),
        )
        boundary = classify_boundary(envelope)
        graph = Profiler().profile(envelope)
        advice = AdvisoryEngine().advise(envelope, boundary, graph)
        workers = [c for c in advice.candidates if c.mode == "workers"][0]
        assert workers.worker_count == 2
        assert not workers.rejection_codes

    def test_material_amendment_with_write_regions_is_rejected(self):
        envelope = make_envelope(
            host_declared_request_type="material_amendment",
            permissions=RoutingPermissions(
                requested_write_regions=[
                    {"resource_kind": "file", "resource_id": "src/a.py", "selector_type": "whole_resource", "selector_value": "*", "base_revision": "abc123"}
                ]
            ),
            resource_summary=RoutingResourceSummary(
                canonical_uri="file:///repos/vr",
                base_revision="abc123",
                referenced_paths=["src/a.py"],
            ),
        )
        boundary = classify_boundary(envelope)
        graph = Profiler().profile(envelope)
        advice = AdvisoryEngine().advise(envelope, boundary, graph)
        assert advice.rejected is True
        assert any("re-plan" in reason for reason in advice.rejection_reasons)

    def test_direct_candidate_is_always_available_baseline(self):
        envelope = make_envelope()
        boundary = classify_boundary(envelope)
        graph = Profiler().profile(envelope)
        advice = AdvisoryEngine().advise(envelope, boundary, graph)
        assert any(c.mode == "direct" and c.worker_count == 0 for c in advice.candidates)

    def test_candidates_carry_fr105_cost_and_latency_estimates(self):
        envelope = make_envelope(user_text="refactor the worker admission path")
        boundary = classify_boundary(envelope)
        graph = Profiler().profile(envelope)
        advice = AdvisoryEngine().advise(envelope, boundary, graph)
        for candidate in advice.candidates:
            assert candidate.estimated_input_tokens is not None
            assert candidate.estimated_output_tokens is not None
            assert candidate.estimated_cost_usd is not None
            assert candidate.estimated_latency_ms is not None
            assert candidate.basis_version == "0.0.2"


class TestShadowLedger:
    def test_envelope_hash_is_stable_and_content_addressing(self):
        first = envelope_hash({"a": 1, "b": [1, 2]})
        second = envelope_hash({"b": [1, 2], "a": 1})
        assert first == second
        assert first != envelope_hash({"a": 1, "b": [1, 3]})

    def test_shadow_record_is_durable_across_store_replay(self, tmp_path):
        journal = LocalJournal(tmp_path)
        try:
            store = Store(journal)
            ledger = ShadowLedger(store)
            envelope = make_envelope()
            advice, _ = RoutingService(store).analyze(envelope)
            record = ledger.record(advice, envelope.model_dump(mode="json"))
            assert store.shadow_records[record.id].id == record.id
        finally:
            journal.close()

        reopened = LocalJournal(tmp_path)
        try:
            store = Store(reopened)
            assert record.id in store.shadow_records
            assert store.shadow_records[record.id].status == "proposed"
        finally:
            reopened.close()

    def test_mark_compared_records_real_outcome(self, tmp_path):
        journal = LocalJournal(tmp_path)
        try:
            store = Store(journal)
            envelope = make_envelope()
            advice, _ = RoutingService(store).analyze(envelope)
            record = ShadowLedger(store).record(advice, envelope.model_dump(mode="json"))
            updated = ShadowLedger(store).mark_compared(record.id, "task/pt_1")
            assert updated.status == "compared"
            assert updated.real_outcome_ref == "task/pt_1"
        finally:
            journal.close()


class TestRoutingService:
    def test_analyze_returns_advice_and_boundary(self, tmp_path):
        store = Store(LocalJournal(tmp_path))
        envelope = make_envelope(host_declared_request_type="new_task")
        advice, boundary = RoutingService(store).analyze(envelope)
        assert advice.envelope_id == envelope.id
        assert boundary.request_type == "new_task"

    def test_analyze_and_shadow_is_idempotent_with_key(self, tmp_path):
        journal = LocalJournal(tmp_path)
        try:
            store = Store(journal)
            service = RoutingService(store)
            envelope = make_envelope()
            envelope_json = envelope.model_dump(mode="json")
            key = ("valueroute", "advisory", "k1")
            first_advice, first_record = service.analyze_and_shadow(envelope, envelope_json, key=key)
            second_advice, second_record = service.analyze_and_shadow(envelope, envelope_json, key=key)
            assert second_record.id == first_record.id
            assert second_advice.id == first_advice.id
        finally:
            journal.close()

    def test_advisory_mode_never_mutates_controller_state(self, tmp_path):
        journal = LocalJournal(tmp_path)
        try:
            store = Store(journal)
            service = RoutingService(store)
            envelope = make_envelope()
            service.analyze_and_shadow(envelope, envelope.model_dump(mode="json"))
            assert store.sessions == {}
            assert store.tasks == {}
            assert store.plans == {}
            assert store.leases == {}
        finally:
            journal.close()

    def test_list_shadow_returns_recorded_entries(self, tmp_path):
        journal = LocalJournal(tmp_path)
        try:
            store = Store(journal)
            service = RoutingService(store)
            envelope = make_envelope()
            service.analyze_and_shadow(envelope, envelope.model_dump(mode="json"))
            assert len(service.list_shadow()) == 1
            assert service.list_shadow()[0].envelope_hash
        finally:
            journal.close()
