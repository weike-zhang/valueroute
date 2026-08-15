from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrchestrationMode(str, Enum):
    off = "off"
    worker_only = "worker_only"
    advisory = "advisory"
    automatic = "automatic"


class TaskStatus(str, Enum):
    draft = "draft"
    planned = "planned"
    queued = "queued"
    running = "running"
    waiting_approval = "waiting_approval"
    pause_requested = "pause_requested"
    cancel_requested = "cancel_requested"
    paused = "paused"
    completed = "completed"
    partial = "partial"
    blocked = "blocked"
    failed = "failed"
    cancelled = "cancelled"


class ObservationStatus(str, Enum):
    observed_pass = "observed_pass"
    observed_fail = "observed_fail"
    unobserved = "unobserved"
    not_applicable = "not_applicable"


class ReviewStatus(str, Enum):
    submitted = "submitted"
    accepted = "accepted"
    rejected = "rejected"


class VerificationStatus(str, Enum):
    passed = "passed"
    blocked = "blocked"
    failed = "failed"


class WorkerAttemptStatus(str, Enum):
    queued = "queued"
    claimed = "claimed"
    running = "running"
    waiting_approval = "waiting_approval"
    pause_requested = "pause_requested"
    cancel_requested = "cancel_requested"
    paused = "paused"
    succeeded = "succeeded"
    partial = "partial"
    blocked = "blocked"
    failed = "failed"
    cancelled = "cancelled"


class ExecutionRequest(StrictModel):
    """The complete provider input needed to replay one execution attempt.

    This deliberately excludes process-local controls such as cancellation
    handles.  Those are reconstructed by the runner after the durable request
    has been recovered.
    """

    task_id: str = Field(min_length=1, max_length=200)
    input_text: str = Field(min_length=1, max_length=100000)
    reasoning_effort: str = Field(default="medium", min_length=1, max_length=40)
    retries: int = Field(default=0, ge=0, le=100)


class IntegrationAttemptStatus(str, Enum):
    queued = "queued"
    running = "running"
    integrated = "integrated"
    conflicted = "conflicted"
    rejected = "rejected"


class Acceptance(StrictModel):
    id: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=2000)
    required: bool = True


class Workspace(StrictModel):
    adapter_id: str = "local"
    canonical_uri: str = Field(min_length=1, max_length=2000)
    base_revision: str = Field(min_length=1, max_length=512)


class WorkspaceBinding(StrictModel):
    """The only workspace a WorkerAttempt is allowed to execute against."""

    owner_id: str = Field(min_length=1, max_length=200)
    owner_workspace: str = Field(min_length=1, max_length=4000)
    canonical_uri: str = Field(min_length=1, max_length=2000)
    base_revision: str = Field(min_length=1, max_length=512)


class Budgets(StrictModel):
    max_input_tokens: int | None = Field(default=None, ge=0)
    max_output_tokens: int | None = Field(default=None, ge=0)
    max_total_tokens: int | None = Field(default=None, ge=0)
    max_cost_usd: float | None = Field(default=None, ge=0)
    max_wall_time_seconds: float | None = Field(default=None, ge=0)


class ControllerSession(StrictModel):
    id: str
    version: int = 1
    tenant_id: str
    host_session_id: str
    orchestration_mode: OrchestrationMode
    active_controller_epoch_id: str | None = None
    max_active_workers: int = Field(default=5, ge=0, le=5)
    created_at: datetime = Field(default_factory=now)


class ControllerEpoch(StrictModel):
    id: str
    version: int = 1
    controller_session_id: str
    provider_id: str
    model_id: str
    model_snapshot: str | None = None
    reasoning_effort: str
    status: Literal["active", "released"] = "active"
    activated_at: datetime = Field(default_factory=now)


class EvidenceRecord(StrictModel):
    id: str
    requirement_id: str
    evidence_type: Literal["test", "static_check", "live_check", "artifact", "human_confirmation"]
    observation_status: ObservationStatus
    source: str = Field(min_length=1, max_length=2000)
    artifact_ref: str | None = None
    child_task_id: str | None = Field(default=None, min_length=1, max_length=200)
    region: ResourceRegion | None = None
    recorded_at: datetime = Field(default_factory=now)


class OwnerSelfReview(StrictModel):
    """The Owner's durable self-review for exactly one owned boundary."""

    id: str = Field(min_length=1, max_length=200)
    version: int = Field(default=1, ge=1)
    child_task_id: str = Field(min_length=1, max_length=200)
    owner_agent_id: str = Field(min_length=1, max_length=200)
    review_regions: tuple[ResourceRegion, ...] = Field(min_length=1)
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=4000)
    status: ReviewStatus = ReviewStatus.submitted
    rejection_reason: str | None = Field(default=None, max_length=2000)
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class VerificationRecord(StrictModel):
    """Durable verifier result; absence or unobserved evidence never passes."""

    id: str = Field(min_length=1, max_length=200)
    version: int = Field(default=1, ge=1)
    child_task_id: str = Field(min_length=1, max_length=200)
    review_id: str = Field(min_length=1, max_length=200)
    verifier_agent_id: str = Field(min_length=1, max_length=200)
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    status: VerificationStatus
    reasons: tuple[str, ...] = Field(default_factory=tuple)
    checked_regions: tuple[ResourceRegion, ...] = Field(min_length=1)
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class ParentCompletionEvidence(StrictModel):
    """Facts that must be present before a parent task can be completed."""

    child_statuses: list[TaskStatus] = Field(default_factory=list)
    changes_integrated: bool = False
    parent_verification_passed: bool = False


class ParentTask(StrictModel):
    id: str
    version: int = 1
    controller_session_id: str
    request_type: Literal["new_task", "material_amendment", "continuation", "clarification", "control"]
    goal: str = Field(min_length=1, max_length=10000)
    acceptance_contract: list[Acceptance] = Field(min_length=1, max_length=100)
    data_classification: Literal["public", "internal", "confidential", "restricted"]
    workspace: Workspace
    budgets: Budgets = Field(default_factory=Budgets)
    status: TaskStatus = TaskStatus.draft
    plan_id: str | None = None
    child_task_ids: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    latest_checkpoint_id: str | None = None
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class WorkerAttempt(StrictModel):
    """One immutable execution try; retries are represented by a new instance."""

    id: str
    version: int = 1
    worker_session_id: str
    child_task_id: str
    workspace: WorkspaceBinding | None = None
    provider_request: ExecutionRequest | None = None
    status: WorkerAttemptStatus = WorkerAttemptStatus.queued
    resumed_from_attempt_id: str | None = None
    recovery_checkpoint_id: str | None = None
    claim_token: str | None = None
    claim_expires_at: datetime | None = None
    checkpoint_id: str | None = None
    checkpoint_safe_to_resume: bool = False
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class WorkerAttemptTransitionConditions(StrictModel):
    """Persistable facts required by guarded WorkerAttempt transitions."""

    capacity_available: bool = False
    claim_token_valid: bool = False
    checkpoint_durable: bool = False
    execution_stopped: bool = False


class ChildProposal(StrictModel):
    client_ref: str = Field(min_length=1, max_length=120)
    objective: str = Field(min_length=1, max_length=4000)
    depends_on: list[str] = Field(default_factory=list)
    read_scope: list[str] = Field(default_factory=list)
    write_regions: list[dict[str, Any]] = Field(default_factory=list)
    acceptance_contract: list[str] = Field(min_length=1, max_length=50)
    requested_model_profile: str = "default_worker"


class WorkerPlanProposal(StrictModel):
    expected_parent_version: int = Field(ge=1)
    children: list[ChildProposal] = Field(max_length=5)
    integration_order: list[str] = Field(default_factory=list)


class PlanValidationIssue(StrictModel):
    severity: Literal["error", "warning"]
    code: str
    path: str
    message: str
    remediation: str | None = None


class PlanValidationResult(StrictModel):
    valid: bool
    proposal_hash: str
    issues: list[PlanValidationIssue] = Field(default_factory=list)
    normalized_plan_id: str | None = None


class WorkerPlan(StrictModel):
    id: str
    parent_task_id: str
    proposal_hash: str
    children: list[ChildProposal]
    integration_order: list[str]
    version: int = 1


class ResourceRegion(StrictModel):
    resource_kind: Literal["file", "directory", "database", "external"]
    resource_id: str = Field(min_length=1, max_length=2000)
    selector_type: Literal["symbol", "ast_node", "path_prefix", "row_keys", "key_range", "partition", "json_pointer", "provider_subresource", "whole_resource"]
    selector_value: Any
    base_revision: str = Field(min_length=1, max_length=512)


class WriterLease(StrictModel):
    id: str
    version: int = 1
    child_task_id: str
    owner_agent_id: str
    region: ResourceRegion
    status: Literal["active", "released", "expired", "revoked"] = "active"
    acquired_at: datetime = Field(default_factory=now)
    expires_at: datetime | None = None


class IntegrationAttempt(StrictModel):
    """One durable, ordered attempt to integrate a ChangeSet.

    A new instance represents a retry.  Terminal attempts therefore remain in
    the journal instead of being overwritten by a later integration attempt.
    """

    id: str
    version: int = 1
    parent_task_id: str | None = None
    client_ref: str = Field(min_length=1, max_length=120)
    order_index: int = Field(default=0, ge=0)
    owner_id: str | None = None
    base_revision: str | None = None
    status: IntegrationAttemptStatus = IntegrationAttemptStatus.queued
    code: str | None = None
    message: str | None = None
    revision: str | None = None
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class TaskView(StrictModel):
    id: str
    version: int
    controller_session_id: str
    status: TaskStatus
    goal: str
    plan_id: str | None
    child_tasks: list[str]
    integration_status: Literal["pending", "running", "integrated", "conflicted", "rejected"] = "pending"
    acceptance_summary: dict[str, int]
    latest_checkpoint_id: str | None = None
    created_at: datetime
    updated_at: datetime
