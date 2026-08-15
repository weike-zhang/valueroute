from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator

from valueroute.domain.models import StrictModel, now, new_id


RequestType = Literal["new_task", "material_amendment", "continuation", "clarification", "control"]


class RoutingPermissions(StrictModel):
    """The minimal permission summary a Profiler is allowed to read.

    It is a read-only snapshot of what the host says the request may access.
    The Profiler must not use it to derive write rights for anyone.
    """

    read_scope: list[str] = Field(default_factory=list, max_length=200)
    available_tools: list[str] = Field(default_factory=list, max_length=200)
    requested_write_regions: list[dict[str, Any]] = Field(default_factory=list, max_length=200)


class RoutingResourceSummary(StrictModel):
    """Verifiable identity of the resource the request would touch."""

    canonical_uri: str = Field(min_length=1, max_length=2000)
    base_revision: str = Field(min_length=1, max_length=512)
    referenced_paths: list[str] = Field(default_factory=list, max_length=500)
    referenced_symbols: list[str] = Field(default_factory=list, max_length=500)


class RoutingRequestEnvelope(StrictModel):
    """The only input a Profiler may read.

    It deliberately isolates the user text, the permission summary, and the
    resource summary so the Profiler has no execution rights and no access to
    the full controller state.  The host decides what is placed here.
    """

    id: str = Field(default_factory=lambda: new_id("env"), min_length=1, max_length=200)
    tenant_id: str = Field(min_length=1, max_length=200)
    host_session_id: str = Field(min_length=1, max_length=200)
    host_declared_request_type: RequestType | None = None
    user_text: str = Field(min_length=1, max_length=10000)
    permissions: RoutingPermissions = Field(default_factory=RoutingPermissions)
    resource_summary: RoutingResourceSummary | None = None
    data_classification: Literal["public", "internal", "confidential", "restricted"] = "internal"
    created_at: datetime = Field(default_factory=now)

    @field_validator("user_text")
    @classmethod
    def strip_user_text(cls, value: str) -> str:
        return value.strip()


class RequestBoundaryDecision(StrictModel):
    """How ValueRoute classifies the request boundary, with evidence."""

    request_type: RequestType
    confidence: float = Field(ge=0.0, le=1.0)
    method: Literal["host_declared", "rule_based"]
    rationale: str = Field(default="", max_length=2000)


class RequirementNode(StrictModel):
    id: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    required: bool = True
    verification_required: bool = True


class RequirementConstraint(StrictModel):
    id: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    kind: Literal["scope", "data", "tool", "budget"] = "scope"


class EvidenceGap(StrictModel):
    id: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)


class RequirementGraph(StrictModel):
    """Read-only profiler output.

    It describes requirements, constraints, and evidence gaps.  It must never
    contain or imply write permissions; the executor grants rights through the
    normal lease machinery only.
    """

    id: str = Field(default_factory=lambda: new_id("rg"), min_length=1, max_length=200)
    requirements: list[RequirementNode] = Field(default_factory=list, max_length=200)
    constraints: list[RequirementConstraint] = Field(default_factory=list, max_length=200)
    evidence_gaps: list[EvidenceGap] = Field(default_factory=list, max_length=200)
    generated_by: Literal["profiler"] = "profiler"
    profiler_version: str = "0.0.2"
    created_at: datetime = Field(default_factory=now)

    @property
    def has_write_suggestion(self) -> bool:
        """Advisory graphs are read-only; this must always be False."""
        return False


class RoutingCandidate(StrictModel):
    id: str = Field(min_length=1, max_length=200)
    mode: Literal["direct", "workers"]
    worker_count: int = Field(default=0, ge=0, le=5)
    rejection_codes: list[str] = Field(default_factory=list, max_length=20)
    estimated_input_tokens: int | None = Field(default=None, ge=0)
    estimated_output_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    estimated_latency_ms: int | None = Field(default=None, ge=0)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1, max_length=4000)
    basis_version: str = Field(default="0.0.2", min_length=1, max_length=100)


class RoutingAdvice(StrictModel):
    id: str = Field(default_factory=lambda: new_id("adv"), min_length=1, max_length=200)
    envelope_id: str = Field(min_length=1, max_length=200)
    boundary_decision: RequestBoundaryDecision
    requirement_graph: RequirementGraph
    candidates: list[RoutingCandidate] = Field(default_factory=list, max_length=20)
    rejected: bool = False
    rejection_reasons: list[str] = Field(default_factory=list, max_length=20)
    created_at: datetime = Field(default_factory=now)


class ShadowRecord(StrictModel):
    """A durable record of advice that was *not* executed.

    Used for offline comparison against real v0.0.1 results.  The record
    deliberately stores the advice and the request fingerprint, never new
    write permissions or execution rights.
    """

    id: str = Field(default_factory=lambda: new_id("shadow"), min_length=1, max_length=200)
    envelope_hash: str = Field(min_length=1, max_length=128)
    advice: RoutingAdvice
    status: Literal["proposed", "compared"] = "proposed"
    real_outcome_ref: str | None = Field(default=None, max_length=2000)
    created_at: datetime = Field(default_factory=now)


__all__ = [
    "EvidenceGap",
    "RequestBoundaryDecision",
    "RequirementConstraint",
    "RequirementGraph",
    "RequirementNode",
    "RoutingAdvice",
    "RoutingCandidate",
    "RoutingPermissions",
    "RoutingRequestEnvelope",
    "RoutingResourceSummary",
    "ShadowRecord",
]
