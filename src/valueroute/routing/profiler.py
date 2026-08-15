from __future__ import annotations

from valueroute.domain.models import new_id
from valueroute.routing.models import (
    EvidenceGap,
    RequirementConstraint,
    RequirementGraph,
    RequirementNode,
    RoutingRequestEnvelope,
)


class Profiler:
    """Read-only profiler that turns a RoutingRequestEnvelope into a graph.

    It never executes anything, never inspects controller state, and never
    derives write permissions.  The output is a structured, auditable
    RequirementGraph that advisory and shadow modes can reference.
    """

    def __init__(self, *, version: str = "0.0.2") -> None:
        self.version = version

    def profile(self, envelope: RoutingRequestEnvelope) -> RequirementGraph:
        graph = RequirementGraph(id=new_id("rg"), profiler_version=self.version)
        self._requirements(graph, envelope)
        self._constraints(graph, envelope)
        self._evidence_gaps(graph, envelope)
        return graph

    def _requirements(self, graph: RequirementGraph, envelope: RoutingRequestEnvelope) -> None:
        goal = envelope.user_text
        graph.requirements.append(
            RequirementNode(
                id="req_goal",
                description=f"goal: {goal}",
                required=True,
                verification_required=True,
            )
        )
        if envelope.resource_summary is not None and envelope.resource_summary.referenced_paths:
            graph.requirements.append(
                RequirementNode(
                    id="req_resources",
                    description="modify only the referenced resource summary",
                    required=True,
                    verification_required=False,
                )
            )

    def _constraints(self, graph: RequirementGraph, envelope: RoutingRequestEnvelope) -> None:
        if envelope.data_classification:
            graph.constraints.append(
                RequirementConstraint(
                    id="cstr_data_classification",
                    description=f"data classification: {envelope.data_classification}",
                    kind="data",
                )
            )
        if envelope.permissions.read_scope:
            graph.constraints.append(
                RequirementConstraint(
                    id="cstr_read_scope",
                    description=f"read scope is limited to: {', '.join(envelope.permissions.read_scope[:20])}",
                    kind="scope",
                )
            )
        if envelope.permissions.available_tools:
            graph.constraints.append(
                RequirementConstraint(
                    id="cstr_tools",
                    description=f"available tools: {', '.join(envelope.permissions.available_tools[:20])}",
                    kind="tool",
                )
            )

    def _evidence_gaps(self, graph: RequirementGraph, envelope: RoutingRequestEnvelope) -> None:
        if not envelope.user_text:
            graph.evidence_gaps.append(EvidenceGap(id="gap_user_text", description="user text is empty"))
        if not envelope.resource_summary:
            graph.evidence_gaps.append(
                EvidenceGap(id="gap_resource_summary", description="no resource summary; region independence cannot be verified")
            )
        elif not envelope.resource_summary.referenced_paths and not envelope.resource_summary.referenced_symbols:
            graph.evidence_gaps.append(
                EvidenceGap(
                    id="gap_regions",
                    description="resource summary lists no paths or symbols; write regions cannot be proven independent",
                )
            )


__all__ = ["Profiler"]
