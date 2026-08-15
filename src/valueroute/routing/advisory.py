from __future__ import annotations

from valueroute.domain.models import new_id
from valueroute.routing.models import (
    RequestBoundaryDecision,
    RequirementGraph,
    RoutingAdvice,
    RoutingCandidate,
    RoutingRequestEnvelope,
)


class AdvisoryEngine:
    """Pure, advisory-only routing suggestions.

    This engine only computes candidate suggestions.  It never registers a
    controller, never creates a WorkerPlan, and never changes model or policy
    configuration.  All decisions are rule-based and documented in the
    returned rationale, with a stable basis version.
    """

    def __init__(self, *, version: str = "0.0.2") -> None:
        self.version = version
        self.max_workers = 5
        # Conservative rule-based estimate basis for FR-105.  These constants
        # are advisory only and never affect billing or scheduling.
        self.tokens_per_char = 0.25
        self.cost_usd_per_input_token = 0.0000005
        self.cost_usd_per_output_token = 0.0000015
        self.output_token_ratio = 0.5
        self.latency_ms_per_input_token = 0.02

    def estimate(self, text: str, worker_count: int) -> tuple[int, int, float, int]:
        """Estimate (input_tokens, output_tokens, cost_usd, latency_ms)."""
        input_tokens = int(len(text) * self.tokens_per_char) + 100
        output_tokens = int(input_tokens * self.output_token_ratio)
        cost = input_tokens * self.cost_usd_per_input_token + output_tokens * self.cost_usd_per_output_token
        latency = int(input_tokens * self.latency_ms_per_input_token) * max(1, worker_count)
        return input_tokens, output_tokens, round(cost, 6), latency

    def advise(
        self,
        envelope: RoutingRequestEnvelope,
        boundary: RequestBoundaryDecision,
        graph: RequirementGraph,
    ) -> RoutingAdvice:
        candidates: list[RoutingCandidate] = []
        rejected = False
        rejection_reasons: list[str] = []

        # A control or clarification request is never delegated.
        if boundary.request_type in {"control", "clarification"}:
            rejected = True
            rejection_reasons.append(f"{boundary.request_type} requests are not candidate work for delegation")

        direct = self._direct_candidate(envelope.user_text)
        candidates.append(direct)

        if not rejected and graph.requirements:
            worker = self._worker_candidate(envelope, boundary, graph)
            if worker.rejection_codes:
                rejection_reasons.extend(worker.rejection_codes)
                rejected = True
            else:
                candidates.append(worker)

        return RoutingAdvice(
            id=new_id("adv"),
            envelope_id=envelope.id,
            boundary_decision=boundary,
            requirement_graph=graph,
            candidates=candidates,
            rejected=rejected,
            rejection_reasons=list(dict.fromkeys(rejection_reasons)),
        )

    def _direct_candidate(self, user_text: str) -> RoutingCandidate:
        input_tokens, output_tokens, cost, latency = self.estimate(user_text, 0)
        return RoutingCandidate(
            id=new_id("cand"),
            mode="direct",
            worker_count=0,
            estimated_input_tokens=input_tokens,
            estimated_output_tokens=output_tokens,
            estimated_cost_usd=cost,
            estimated_latency_ms=latency,
            confidence=0.9,
            rationale="host controller executes directly with zero workers; this is the always-available baseline",
            basis_version=self.version,
        )

    def _worker_candidate(self, envelope: RoutingRequestEnvelope, boundary: RequestBoundaryDecision, graph: RequirementGraph) -> RoutingCandidate:
        rejected: list[str] = []
        worker_count = 0
        regions = envelope.permissions.requested_write_regions
        summary = envelope.resource_summary

        if boundary.request_type == "material_amendment" and regions:
            rejected.append("amendments touch existing ownership and must not be delegated without a re-plan")
        if not regions:
            rejected.append("no declared write regions means we cannot prove any delegation is safe")
        else:
            worker_count = min(len(regions), self.max_workers)
            if len(regions) > self.max_workers:
                worker_count = self.max_workers
                rejected.append(f"more than {self.max_workers} write regions exceed the worker cap")
        if summary is None or (not summary.referenced_paths and not summary.referenced_symbols):
            rejected.append("no resource summary means region independence cannot be proven; fail closed")
        if worker_count == 0:
            rejected.append("no workers are safely schedulable from this envelope")

        if rejected:
            input_tokens, output_tokens, cost, latency = self.estimate(envelope.user_text, 0)
            return RoutingCandidate(
                id=new_id("cand"),
                mode="workers",
                worker_count=0,
                estimated_input_tokens=input_tokens,
                estimated_output_tokens=output_tokens,
                estimated_cost_usd=cost,
                estimated_latency_ms=latency,
                rejection_codes=rejected,
                confidence=0.2,
                rationale="worker delegation rejected by conservative advisory rules; see rejection_codes",
                basis_version=self.version,
            )
        input_tokens, output_tokens, cost, latency = self.estimate(envelope.user_text, worker_count)
        return RoutingCandidate(
            id=new_id("cand"),
            mode="workers",
            worker_count=worker_count,
            estimated_input_tokens=input_tokens,
            estimated_output_tokens=output_tokens,
            estimated_cost_usd=cost,
            estimated_latency_ms=latency,
            confidence=0.8,
            rationale=f"{worker_count} write region(s) declared with a resource summary; conservative advisory split",
            basis_version=self.version,
        )


__all__ = ["AdvisoryEngine"]
