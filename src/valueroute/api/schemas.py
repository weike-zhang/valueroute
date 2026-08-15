from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr

from valueroute.approvals import ApprovalDecision
from valueroute.approvals import ApprovalStatus
from valueroute.domain.models import (
    ControllerEpoch,
    ControllerSession,
    EvidenceRecord,
    IntegrationAttempt,
    ObservationStatus,
    OrchestrationMode,
    OwnerSelfReview,
    ParentTask,
    PlanValidationResult,
    TaskView,
    VerificationRecord,
    WorkerPlan,
    WriterLease,
)
from valueroute.observability.usage import TaskUsageReport
from valueroute.ownership.boundaries import ChildTaskBoundary, OwnerAssignment


T = TypeVar("T")


class RequestModel(BaseModel):
    """API input models reject unknown fields and type coercion at boundaries."""

    model_config = ConfigDict(extra="forbid")


class AcceptanceRequest(RequestModel):
    id: StrictStr = Field(min_length=1, max_length=120)
    description: StrictStr = Field(min_length=1, max_length=2000)
    required: StrictBool = True


class WorkspaceRequest(RequestModel):
    adapter_id: StrictStr = Field(default="local", min_length=1, max_length=120)
    canonical_uri: StrictStr = Field(min_length=1, max_length=2000)
    base_revision: StrictStr = Field(min_length=1, max_length=512)


class BudgetsRequest(RequestModel):
    max_input_tokens: StrictInt | None = Field(default=None, ge=0)
    max_output_tokens: StrictInt | None = Field(default=None, ge=0)
    max_total_tokens: StrictInt | None = Field(default=None, ge=0)
    max_cost_usd: StrictFloat | None = Field(default=None, ge=0)
    max_wall_time_seconds: StrictFloat | None = Field(default=None, ge=0)


class ResourceRegionRequest(RequestModel):
    resource_kind: Literal["file", "directory", "database", "external"]
    resource_id: StrictStr = Field(min_length=1, max_length=2000)
    selector_type: Literal["symbol", "ast_node", "path_prefix", "row_keys", "key_range", "partition", "json_pointer", "provider_subresource", "whole_resource"]
    selector_value: Any
    base_revision: StrictStr = Field(min_length=1, max_length=512)


class CreateSessionRequest(RequestModel):
    tenant_id: StrictStr = Field(min_length=1, max_length=200)
    host_session_id: StrictStr = Field(min_length=1, max_length=200)
    orchestration_mode: OrchestrationMode


class RegisterEpochRequest(RequestModel):
    expected_version: StrictInt = Field(ge=1)
    provider_id: StrictStr = Field(min_length=1, max_length=200)
    model_id: StrictStr = Field(min_length=1, max_length=500)
    model_snapshot: StrictStr | None = Field(default=None, max_length=500)
    reasoning_effort: StrictStr = Field(min_length=1, max_length=100)


class CreateTaskRequest(RequestModel):
    controller_session_id: StrictStr = Field(min_length=1, max_length=200)
    request_type: Literal["new_task", "material_amendment", "continuation", "clarification", "control"]
    goal: StrictStr = Field(min_length=1, max_length=10000)
    acceptance_contract: list[AcceptanceRequest] = Field(min_length=1, max_length=100)
    data_classification: Literal["public", "internal", "confidential", "restricted"]
    workspace: WorkspaceRequest
    budgets: BudgetsRequest = Field(default_factory=BudgetsRequest)


class ChangeSetRequest(RequestModel):
    integrated: StrictBool = False
    conflict: StrictBool = False


class VerifyTaskRequest(RequestModel):
    expected_version: StrictInt = Field(ge=1)
    changesets: list[ChangeSetRequest] = Field(default_factory=list, max_length=100)


class RecordEvidenceRequest(RequestModel):
    expected_version: StrictInt = Field(ge=1)
    requirement_id: StrictStr = Field(min_length=1, max_length=120)
    evidence_type: Literal["test", "static_check", "live_check", "artifact", "human_confirmation"]
    observation_status: ObservationStatus
    source: StrictStr = Field(min_length=1, max_length=2000)
    artifact_ref: StrictStr | None = Field(default=None, max_length=2000)
    child_task_id: StrictStr | None = Field(default=None, max_length=200)
    region: ResourceRegionRequest | None = None


class RequestApprovalRequest(RequestModel):
    action_summary: StrictStr = Field(min_length=1, max_length=4000)
    risk: StrictStr = Field(min_length=1, max_length=1000)
    expires_at: datetime
    allowed_decisions: list[ApprovalDecision] = Field(
        default_factory=lambda: [ApprovalDecision.approve, ApprovalDecision.reject],
        min_length=1,
        max_length=2,
    )


class DecideApprovalRequest(RequestModel):
    expected_version: StrictInt = Field(ge=1)
    decision: ApprovalDecision
    reason: StrictStr | None = Field(default=None, min_length=1, max_length=4000)


class ControlTaskRequest(RequestModel):
    expected_version: StrictInt = Field(ge=1)
    reason: StrictStr | None = Field(default=None, min_length=1, max_length=4000)


class ChildProposalRequest(RequestModel):
    client_ref: StrictStr = Field(min_length=1, max_length=120)
    objective: StrictStr = Field(min_length=1, max_length=4000)
    depends_on: list[StrictStr] = Field(default_factory=list, max_length=5)
    read_scope: list[StrictStr] = Field(default_factory=list, max_length=100)
    write_regions: list[ResourceRegionRequest] = Field(default_factory=list, max_length=100)
    acceptance_contract: list[StrictStr] = Field(min_length=1, max_length=50)
    requested_model_profile: StrictStr = Field(default="default_worker", min_length=1, max_length=200)


class SubmitPlanRequest(RequestModel):
    expected_parent_version: StrictInt = Field(ge=1)
    children: list[ChildProposalRequest] = Field(max_length=5)
    integration_order: list[StrictStr] = Field(default_factory=list, max_length=5)


class OwnerReviewRequest(RequestModel):
    expected_assignment_version: StrictInt = Field(ge=1)
    owner_agent_id: StrictStr = Field(min_length=1, max_length=200)
    review_regions: list[ResourceRegionRequest] = Field(min_length=1, max_length=100)
    evidence_ids: list[StrictStr] = Field(min_length=1, max_length=100)
    summary: StrictStr = Field(min_length=1, max_length=4000)


class VerifyReviewRequest(RequestModel):
    expected_review_version: StrictInt = Field(ge=1)
    verifier_agent_id: StrictStr = Field(min_length=1, max_length=200)
    evidence_ids: list[StrictStr] = Field(default_factory=list, max_length=100)


class ResponseMeta(BaseModel):
    """Stable metadata carried by every JSON resource response."""

    model_config = ConfigDict(extra="forbid")

    request_id: StrictStr
    resource_version: StrictInt | None = None


class ResponseEnvelope(BaseModel, Generic[T]):
    """The v1 success envelope; errors use the existing detail contract."""

    model_config = ConfigDict(extra="forbid")

    data: T
    meta: ResponseMeta
    error: Literal[None] = None


class ChildListData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    children: list[ChildTaskBoundary]
    assignments: list[OwnerAssignment]


class ReviewListData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviews: list[OwnerSelfReview]


class VerificationListData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verifications: list[VerificationRecord]


class ApprovalListData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approvals: list[ApprovalView]


class IntegrationAttemptsData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempts: list[IntegrationAttempt]


class IntegrationResultItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_ref: StrictStr
    status: Literal["integrated", "blocked", "queued", "running"]
    code: StrictStr | None = None
    message: StrictStr | None = None
    owner_id: StrictStr | None = None
    revision: StrictStr | None = None


class IntegrationResultData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: list[IntegrationResultItem]
    results: list[IntegrationResultItem]


class LeaseListData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    leases: list[WriterLease]


class TaskVerificationData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: StrictStr
    version: StrictInt
    status: StrictStr
    reasons: list[StrictStr]
    can_complete: StrictBool


class EvidenceGateData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    can_complete: StrictBool
    missing_required: list[StrictStr]
    failed_required: list[StrictStr]
    unobserved_required: list[StrictStr]


class EvidenceResponseData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence: EvidenceRecord
    gate: EvidenceGateData


class EvidenceListData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    records: list[EvidenceRecord]
    gate: EvidenceGateData


class ApprovalView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: StrictStr
    version: StrictInt = Field(ge=1)
    action_summary: StrictStr
    risk: StrictStr
    expires_at: datetime
    allowed_decisions: list[ApprovalDecision]
    status: ApprovalStatus
    decision: ApprovalDecision | None = None
    reason: StrictStr | None = None
    decided_at: datetime | None = None


class PlanResponseData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: WorkerPlan
    validation: PlanValidationResult


class SessionResponse(ResponseEnvelope[ControllerSession]):
    pass


class EpochResponse(ResponseEnvelope[ControllerEpoch]):
    pass


class TaskResponse(ResponseEnvelope[ParentTask]):
    pass


class TaskViewResponse(ResponseEnvelope[TaskView]):
    pass


class ChildListResponse(ResponseEnvelope[ChildListData]):
    pass


class ChildResponse(ResponseEnvelope[ChildTaskBoundary]):
    pass


class ReviewListResponse(ResponseEnvelope[ReviewListData]):
    pass


class VerificationListResponse(ResponseEnvelope[VerificationListData]):
    pass


class ApprovalListResponse(ResponseEnvelope[ApprovalListData]):
    pass


class IntegrationAttemptsResponse(ResponseEnvelope[IntegrationAttemptsData]):
    pass


class IntegrationResultResponse(ResponseEnvelope[IntegrationResultData]):
    pass


class LeaseListResponse(ResponseEnvelope[LeaseListData]):
    pass


class TaskVerificationResponse(ResponseEnvelope[TaskVerificationData]):
    pass


class EvidenceResponse(ResponseEnvelope[EvidenceResponseData]):
    pass


class EvidenceWriteResponse(ResponseEnvelope[EvidenceResponseData | ParentTask]):
    """First write returns evidence; an idempotent replay may return its task event."""

    pass


class EvidenceListResponse(ResponseEnvelope[EvidenceListData]):
    pass


class ReviewResponse(ResponseEnvelope[OwnerSelfReview]):
    pass


class VerificationResponse(ResponseEnvelope[VerificationRecord]):
    pass


class UsageResponse(ResponseEnvelope[TaskUsageReport]):
    pass


class ApprovalResponse(ResponseEnvelope[ApprovalView]):
    pass


class PlanResponse(ResponseEnvelope[PlanResponseData]):
    pass
