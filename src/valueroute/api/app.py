from __future__ import annotations

import asyncio
import csv
import io
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from valueroute.api.schemas import (
    AdvisoryRequest,
    AdvisoryResponse,
    ApprovalListResponse,
    ApprovalResponse,
    ApprovalView,
    ChildListResponse,
    ChildResponse,
    ControlTaskRequest,
    CreateSessionRequest,
    CreateTaskRequest,
    DecideApprovalRequest,
    EnsureControllerRequest,
    EpochResponse,
    EvidenceListResponse,
    EvidenceWriteResponse,
    IntegrationAttemptsResponse,
    IntegrationResultResponse,
    LeaseListResponse,
    OwnerReviewRequest,
    PlanResponse,
    RecordEvidenceRequest,
    RegisterEpochRequest,
    RequestApprovalRequest,
    ReviewListResponse,
    ReviewResponse,
    SessionResponse,
    ShadowListResponse,
    SubmitPlanRequest,
    SwitchControllerRequest,
    TaskResponse,
    TaskVerificationResponse,
    TaskViewResponse,
    UsageResponse,
    VerificationListResponse,
    VerificationResponse,
    VerifyReviewRequest,
    VerifyTaskRequest,
)
from valueroute.api.trace_ui import render_trace_page
from valueroute.application.control import ControlService
from valueroute.application.service import DomainError, Service
from valueroute.approvals import (
    Approval,
    ApprovalDecisionConflict,
    ApprovalDecisionNotAllowed,
    ApprovalExpired,
    ApprovalService,
    ApprovalVersionConflict,
)
from valueroute.domain.models import *
from valueroute.domain.models import EvidenceRecord, ParentCompletionEvidence, WorkerAttemptStatus, new_id
from valueroute.domain.state_machine import StateTransitionError, transition_task
from valueroute.evidence import EvidenceGate
from valueroute.evidence.verifier import VerifierService
from valueroute.execution.manager import ExecutionManager
from valueroute.execution.supervisor import ExecutionSupervisor
from valueroute.integration.parent_verification import ChangeSetResult, ChildTaskResult, ParentVerification
from valueroute.observability.events import EventStreamError, deduplicate_events, format_sse_frame, parse_last_event_id
from valueroute.observability.usage import USAGE_EXPORT_FIELDS, build_usage_report, usage_export_rows
from valueroute.ownership.boundaries import OwnerAssignment
from valueroute.ownership.persistence import PersistentOwnershipBoundaryService
from valueroute.ownership.review import OwnerReviewService
from valueroute.routing.automatic import AutomaticControllerService
from valueroute.routing.models import RoutingRequestEnvelope
from valueroute.routing.service import RoutingService
from valueroute.settings import RuntimeProtectionConfig, RuntimeProtectionError, data_dir, ensure_storage_capacity
from valueroute.storage.artifacts import ArtifactStore as LocalArtifactStore
from valueroute.storage.checkpoints import CheckpointStore as LocalCheckpointStore
from valueroute.storage.interfaces import ArtifactStore, CheckpointStore, StateStore
from valueroute.storage.journal import JournalError, LocalJournal
from valueroute.storage.store import Store


def envelope(data: Any, request_id: str = "req_local", version: int | None = None) -> dict[str, Any]:
    return {"data": data, "meta": {"request_id": request_id, "resource_version": version}, "error": None}


def validation_error_code(error: RequestValidationError) -> str:
    expected_fields = {"expected_version", "expected_parent_version"}
    for item in error.errors():
        if set(item.get("loc", ())) & expected_fields:
            return "invalid_boundary" if item.get("type") == "missing" else "invalid_expected_version"
    return "invalid_request"


def create_app(
    root: Path | None = None,
    provider: Any | None = None,
    *,
    workspace_adapter: Any | None = None,
    execution_queue: Any | None = None,
    state_store: StateStore | None = None,
    artifact_store: ArtifactStore | None = None,
    checkpoint_store: CheckpointStore | None = None,
    controller_profiles: list[Any] | None = None,
) -> FastAPI:
    app = FastAPI(title="ValueRoute", version="0.0.1")

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(_: Request, error: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "code": validation_error_code(error),
                    "message": "request validation failed",
                    "errors": jsonable_encoder(error.errors()),
                }
            },
        )

    limits = RuntimeProtectionConfig.from_environment()
    journal = LocalJournal(root or data_dir(), max_bytes=limits.max_journal_bytes, min_free_bytes=limits.min_free_disk_bytes)
    resolved_artifact_store = artifact_store or LocalArtifactStore(
        journal.root, max_bytes=limits.max_artifact_bytes, min_free_bytes=limits.min_free_disk_bytes
    )
    resolved_checkpoint_store = checkpoint_store or LocalCheckpointStore(
        journal.root, resolved_artifact_store, max_bytes=limits.max_checkpoint_bytes, min_free_bytes=limits.min_free_disk_bytes
    )
    store = state_store or Store(journal, resolved_checkpoint_store)
    def append_ownership(event: dict[str, Any]) -> None:
        journal.append([event])
        if event["type"] in {"ownership.owner_assigned", "ownership.owner_transferred", "ownership.owner_released"}:
            assignment = OwnerAssignment.model_validate(event["data"])
            store.assignments[assignment.child_task_id] = assignment

    ownership = PersistentOwnershipBoundaryService(append_ownership, journal.events)
    service = Service(store, ownership)
    control = ControlService(store, resolved_checkpoint_store)
    execution = ExecutionManager(
        store,
        queue=execution_queue,
        workspace_adapter=workspace_adapter,
    )
    supervisor = ExecutionSupervisor(
        store,
        provider,
        runner_kwargs={"checkpoint_store": resolved_checkpoint_store},
        queue=execution_queue,
        workspace_adapter=workspace_adapter,
    ) if provider is not None else None
    evidence_gate = EvidenceGate()
    approval_service = ApprovalService()
    review_service = OwnerReviewService(store, ownership)
    verifier_service = VerifierService(store, ownership, review_service)
    routing_service = RoutingService(store)
    automatic_controller = AutomaticControllerService(store, profiles=controller_profiles or [])
    app.state.store = store
    app.state.service = service
    app.state.control = control
    app.state.execution = execution
    app.state.artifact_store = resolved_artifact_store
    app.state.checkpoint_store = resolved_checkpoint_store
    app.state.state_store = store
    app.state.ownership = ownership
    app.state.routing = routing_service
    app.state.automatic_controller = automatic_controller
    app.state.runtime_protection = limits
    app.state.supervisor = supervisor
    app.state.supervisor_stop = asyncio.Event()
    app.state.supervisor_task = None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if supervisor is not None:
            app.state.supervisor_stop.clear()
            app.state.supervisor_task = asyncio.create_task(supervisor.run_forever(stop_event=app.state.supervisor_stop))
        try:
            yield
        finally:
            task = app.state.supervisor_task
            if task is not None:
                app.state.supervisor_stop.set()
                await asyncio.gather(task, return_exceptions=True)
                app.state.supervisor_task = None
            journal.close()

    # Resources are constructed after the app so the local data directory can
    # be selected by the factory; install the explicit lifespan context once
    # all state dependencies are available.
    app.router.lifespan_context = lifespan

    @app.get("/v1/health/live")
    def live(): return {"status": "ok"}

    @app.get("/v1/health/ready")
    def ready():
        try:
            ensure_storage_capacity(journal.root, incoming_bytes=0, max_bytes=None, min_free_bytes=limits.min_free_disk_bytes)
        except RuntimeProtectionError as error:
            raise HTTPException(503, detail={"code": "storage_protection", "message": str(error)}) from error
        return {"status": "ready", "data_dir": str(journal.root)}

    @app.get("/v1/supervisor/status")
    def supervisor_status():
        task = app.state.supervisor_task
        return {
            "enabled": supervisor is not None,
            "running": bool(task is not None and not task.done()),
            "max_concurrency": supervisor.max_concurrency if supervisor is not None else 0,
        }

    def idem_key(request: Request, value: str | None) -> tuple[str, str, str]:
        if value is None:
            raise HTTPException(400, detail={"code": "missing_idempotency_key", "message": "Idempotency-Key is required for writes"})
        if not value or len(value) > 255 or value != value.strip() or any(ord(character) < 33 or ord(character) > 126 for character in value):
            raise HTTPException(400, detail={"code": "invalid_idempotency_key", "message": "Idempotency-Key must be 1-255 printable characters without surrounding whitespace"})
        tenant = request.headers.get("X-Tenant-ID", "default")
        return tenant, request.url.path, value

    def handle_error(error: Exception) -> None:
        if isinstance(error, DomainError): raise HTTPException(error.status, detail={"code": error.code, "message": error.message, "retryable": error.status >= 500})
        if isinstance(error, RuntimeProtectionError):
            raise HTTPException(507, detail={"code": "storage_protection", "message": str(error), "retryable": True})
        if isinstance(error, (JournalError, ValueError)):
            code = str(error)
            status = 409 if code == "version_conflict" or code == "idempotency_conflict" else 422
            raise HTTPException(status, detail={"code": code, "message": code})
        raise error

    def task_integration_attempts(task_id: str) -> list[IntegrationAttempt]:
        return sorted(
            (
                attempt
                for attempt in store.integration_attempts.values()
                if attempt.parent_task_id == task_id
            ),
            key=lambda attempt: (attempt.order_index, attempt.created_at, attempt.id),
        )

    def task_integration_results(task: ParentTask) -> list[dict[str, Any]]:
        plan = store.plans.get(task.plan_id) if task.plan_id else None
        attempts = task_integration_attempts(task.id)
        if plan is not None and plan.integration_order:
            return store.recover_integration_results(list(plan.integration_order), task.id)
        return [store.integration_result(attempt) for attempt in attempts]

    def task_integration_facts_for_verification(task: ParentTask, fallback: list[ChangeSetResult]) -> list[Any]:
        """Use only journal-backed integration facts for parent verification.

        A caller-provided ChangeSet summary is advisory input, not evidence that
        an integration occurred. If a plan declares integration work and no
        durable result exists yet, fail closed with one blocked fact per item.
        """
        journal_results = task_integration_results(task)
        plan = store.plans.get(task.plan_id) if task.plan_id else None
        if plan is None or not plan.integration_order:
            return journal_results if journal_results else []
        if not journal_results:
            return [
                {"client_ref": client_ref, "status": "blocked", "code": "integration_incomplete"}
                for client_ref in plan.integration_order
            ]

        if not fallback:
            return journal_results

        by_ref = {item["client_ref"]: item for item in journal_results}
        return [
            by_ref.get(
                client_ref,
                {"client_ref": client_ref, "status": "blocked", "code": "integration_incomplete"},
            )
            for client_ref in plan.integration_order
        ]

    def task_view(task: ParentTask) -> TaskView:
        """Project the durable task aggregate into the stable read view."""
        required = [item for item in task.acceptance_contract if item.required]
        latest = {item.requirement_id: item for item in task.evidence}
        summary = {
            "required": len(required),
            "satisfied": sum(
                1
                for item in required
                if latest.get(item.id) is not None
                and latest[item.id].observation_status in {ObservationStatus.observed_pass, ObservationStatus.not_applicable}
            ),
            "failed": sum(
                1
                for item in required
                if latest.get(item.id) is not None
                and latest[item.id].observation_status is ObservationStatus.observed_fail
            ),
        }
        results = task_integration_results(task)
        statuses = {item.get("status") for item in results}
        integration_status = "pending"
        if "blocked" in statuses:
            integration_status = "conflicted"
        elif results and statuses == {"integrated"}:
            integration_status = "integrated"
        elif results:
            integration_status = "running"
        return TaskView(
            id=task.id,
            version=task.version,
            controller_session_id=task.controller_session_id,
            status=task.status,
            goal=task.goal,
            plan_id=task.plan_id,
            child_tasks=task.child_task_ids,
            integration_status=integration_status,
            acceptance_summary=summary,
            latest_checkpoint_id=task.latest_checkpoint_id,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )

    @app.post("/v1/controller-sessions", status_code=201, response_model=SessionResponse)
    async def create_session(payload: CreateSessionRequest, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        try: result = service.create_session(payload.model_dump(mode="json"), idem_key(request, idempotency_key))
        except Exception as e: handle_error(e)
        return envelope(result.model_dump(mode="json"), version=result.version)

    @app.post("/v1/controller-sessions/{session_id}/epochs", status_code=201, response_model=EpochResponse)
    async def register_epoch(session_id: str, payload: RegisterEpochRequest, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        values = payload.model_dump(mode="json")
        expected = values.pop("expected_version")
        try: result = service.register_epoch(session_id, values, expected, idem_key(request, idempotency_key))
        except Exception as e: handle_error(e)
        return envelope(result.model_dump(mode="json"), version=result.version)

    @app.post("/v1/controller-sessions/{session_id}/epochs/automatic", status_code=200, response_model=EpochResponse)
    async def ensure_automatic_controller(session_id: str, payload: EnsureControllerRequest, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        try:
            result = automatic_controller.ensure_controller(
                session_id,
                expected_version=payload.expected_version,
                reasoning_effort=payload.reasoning_effort,
                idem=idem_key(request, idempotency_key),
            )
        except Exception as e: handle_error(e)
        return envelope(result.model_dump(mode="json"), version=result.version)

    @app.post("/v1/controller-sessions/{session_id}/epochs/switch", status_code=200, response_model=EpochResponse)
    async def switch_automatic_controller(session_id: str, payload: SwitchControllerRequest, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        try:
            result = automatic_controller.switch_controller(
                session_id,
                expected_version=payload.expected_version,
                reasoning_effort=payload.reasoning_effort,
                idem=idem_key(request, idempotency_key),
            )
        except Exception as e: handle_error(e)
        return envelope(result.model_dump(mode="json"), version=result.version)

    @app.get("/v1/controller-sessions/{session_id}", response_model=SessionResponse)
    def get_session(session_id: str):
        result = store.sessions.get(session_id)
        if not result: raise HTTPException(404, detail={"code": "not_found", "message": "controller session not found"})
        return envelope(result.model_dump(mode="json"), version=result.version)

    @app.get("/v1/trace/ui", response_class=HTMLResponse, include_in_schema=False)
    def trace_ui() -> HTMLResponse:
        return HTMLResponse(content=render_trace_page(store), status_code=200)

    @app.post("/v1/tasks", status_code=201, response_model=TaskResponse)
    async def create_task(payload: CreateTaskRequest, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        try: result = service.create_task(payload.model_dump(mode="json"), idem_key(request, idempotency_key))
        except Exception as e: handle_error(e)
        return envelope(result.model_dump(mode="json"), version=result.version)

    @app.get("/v1/tasks/{task_id}", response_model=TaskViewResponse)
    def get_task(task_id: str):
        task = store.tasks.get(task_id)
        if not task: raise HTTPException(404, detail={"code": "not_found", "message": "task not found"})
        view = task_view(task)
        return envelope(view.model_dump(mode="json"), version=view.version)

    @app.get("/v1/tasks/{task_id}/children", response_model=ChildListResponse)
    def get_children(task_id: str):
        task = store.tasks.get(task_id)
        if not task:
            raise HTTPException(404, detail={"code": "not_found", "message": "task not found"})
        children = [store.children[child_id].model_dump(mode="json") for child_id in task.child_task_ids if child_id in store.children]
        assignments = [store.assignments[child_id].model_dump(mode="json") for child_id in task.child_task_ids if child_id in store.assignments]
        return envelope({"children": children, "assignments": assignments}, version=task.version)

    @app.get("/v1/tasks/{task_id}/children/{child_task_id}", response_model=ChildResponse)
    def get_child(task_id: str, child_task_id: str):
        task = store.tasks.get(task_id)
        child = store.children.get(child_task_id)
        if task is None or child is None or child_task_id not in task.child_task_ids:
            raise HTTPException(404, detail={"code": "not_found", "message": "child task not found"})
        return envelope(child.model_dump(mode="json"), version=getattr(child, "version", task.version))

    @app.get("/v1/tasks/{task_id}/integration-attempts", response_model=IntegrationAttemptsResponse)
    def get_integration_attempts(task_id: str):
        task = store.tasks.get(task_id)
        if not task:
            raise HTTPException(404, detail={"code": "not_found", "message": "task not found"})
        attempts = [attempt.model_dump(mode="json") for attempt in task_integration_attempts(task_id)]
        return envelope({"attempts": attempts}, version=task.version)

    @app.get("/v1/tasks/{task_id}/integration-result", response_model=IntegrationResultResponse, response_model_exclude_none=True)
    def get_integration_result(task_id: str):
        task = store.tasks.get(task_id)
        if not task:
            raise HTTPException(404, detail={"code": "not_found", "message": "task not found"})
        results = task_integration_results(task)
        return envelope({"result": results, "results": results}, version=task.version)

    @app.get("/v1/leases", response_model=LeaseListResponse)
    def get_leases(task_id: str | None = None, owner_id: str | None = None, resource_id: str | None = None):
        """Query durable WriterLease state without exposing mutation primitives."""
        leases = [lease for lease in store.leases.values() if (
            (task_id is None or lease.child_task_id == task_id)
            and (owner_id is None or lease.owner_agent_id == owner_id)
            and (resource_id is None or lease.region.resource_id == resource_id)
        )]
        return envelope({"leases": [lease.model_dump(mode="json") for lease in leases]})

    @app.post("/v1/tasks/{task_id}/verify", status_code=202, response_model=TaskVerificationResponse)
    async def verify_task(task_id: str, payload: VerifyTaskRequest, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        task = store.tasks.get(task_id)
        if not task:
            raise HTTPException(404, detail={"code": "not_found", "message": "task not found"})
        values = payload.model_dump(mode="json")
        key = idem_key(request, idempotency_key)
        duplicate = store.check_idempotency(key, values)
        if duplicate:
            return envelope(duplicate["event"]["data"], version=duplicate["event"]["data"]["version"])
        expected = payload.expected_version
        if expected != task.version:
            raise HTTPException(409, detail={"code": "version_conflict", "message": "task version has changed"})
        status_map = {WorkerAttemptStatus.succeeded: "completed", WorkerAttemptStatus.partial: "partial", WorkerAttemptStatus.blocked: "blocked", WorkerAttemptStatus.failed: "failed", WorkerAttemptStatus.cancelled: "cancelled"}
        child_results = [ChildTaskResult(status_map.get(attempt.status, "partial")) for attempt in store.attempts.values() if attempt.child_task_id in task.child_task_ids]
        payload_changesets = [ChangeSetResult(**item) for item in values.get("changesets", [])]
        changesets = task_integration_facts_for_verification(task, payload_changesets)
        gate = evidence_gate.evaluate(task.acceptance_contract, task.evidence)
        verification = ParentVerification().evaluate(child_results, changesets, gate)
        try:
            updated = transition_task(
                task, verification.status, task.version,
                completion_evidence=ParentCompletionEvidence(
                    child_statuses=[TaskStatus.completed for _ in child_results],
                    changes_integrated=verification.can_complete,
                    parent_verification_passed=verification.can_complete,
                ) if verification.status is TaskStatus.completed else None,
            )
        except StateTransitionError as error:
            raise HTTPException(409, detail={"code": "invalid_transition", "message": str(error)}) from error
        store.tasks[task_id] = updated
        event_data = {"id": task_id, "version": updated.version, "status": updated.status.value, "reasons": verification.reasons}
        store.commit({"type": f"task.{updated.status.value}", "data": event_data}, key=key, payload=values)
        store.commit({"type": "task.updated", "data": updated.model_dump(mode="json")})
        return envelope({**event_data, "can_complete": verification.can_complete}, version=updated.version)

    @app.post("/v1/tasks/{task_id}/evidence", status_code=201, response_model=EvidenceWriteResponse)
    async def record_evidence(task_id: str, payload: RecordEvidenceRequest, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        values = payload.model_dump(mode="json")
        key = idem_key(request, idempotency_key)
        task = store.tasks.get(task_id)
        if not task:
            raise HTTPException(404, detail={"code": "not_found", "message": "task not found"})
        expected = payload.expected_version
        request_payload = dict(values)
        duplicate = store.check_idempotency(key, request_payload)
        if duplicate:
            return envelope(duplicate["event"]["data"], version=duplicate["event"]["data"]["version"])
        values.pop("expected_version")
        if expected != task.version:
            raise HTTPException(409, detail={"code": "version_conflict", "message": "task version has changed"})
        record = EvidenceRecord(id=new_id("ev"), **values)
        updated = task.model_copy(update={"evidence": [*task.evidence, record], "version": task.version + 1})
        store.tasks[task_id] = updated
        store.commit({"type": "task.updated", "data": updated.model_dump(mode="json")}, key=key, payload=request_payload)
        gate = evidence_gate.evaluate(task.acceptance_contract, updated.evidence)
        return envelope({"evidence": record.model_dump(mode="json"), "gate": {"can_complete": gate.can_complete, "missing_required": gate.missing_required, "failed_required": gate.failed_required, "unobserved_required": gate.unobserved_required}}, version=updated.version)

    @app.get("/v1/tasks/{task_id}/evidence", response_model=EvidenceListResponse)
    def get_evidence(task_id: str):
        task = store.tasks.get(task_id)
        if not task:
            raise HTTPException(404, detail={"code": "not_found", "message": "task not found"})
        gate = evidence_gate.evaluate(task.acceptance_contract, task.evidence)
        return envelope({"records": [item.model_dump(mode="json") for item in task.evidence], "gate": {"can_complete": gate.can_complete, "missing_required": gate.missing_required, "failed_required": gate.failed_required, "unobserved_required": gate.unobserved_required}}, version=task.version)

    @app.post("/v1/tasks/{task_id}/children/{child_task_id}/review", status_code=201, response_model=ReviewResponse)
    async def submit_owner_review(task_id: str, child_task_id: str, payload: OwnerReviewRequest, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        task = store.tasks.get(task_id)
        if task is None or child_task_id not in task.child_task_ids:
            raise HTTPException(404, detail={"code": "not_found", "message": "child task not found"})
        values = payload.model_dump(mode="json")
        key = idem_key(request, idempotency_key)
        duplicate = store.check_idempotency(key, values)
        if duplicate:
            data = duplicate["event"]["data"]
            return envelope(data, version=data.get("version"))
        try:
            review = review_service.submit(
                child_task_id,
                payload.owner_agent_id,
                [ResourceRegion.model_validate(item.model_dump(mode="json")) for item in payload.review_regions],
                payload.evidence_ids,
                payload.summary,
                expected_assignment_version=payload.expected_assignment_version,
                idem=key,
                payload=values,
            )
        except Exception as error:
            handle_error(error)
        return envelope(review.model_dump(mode="json"), version=review.version)

    @app.post("/v1/tasks/{task_id}/children/{child_task_id}/reviews/{review_id}/verify", status_code=201, response_model=VerificationResponse)
    async def verify_owner_review(task_id: str, child_task_id: str, review_id: str, payload: VerifyReviewRequest, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        task = store.tasks.get(task_id)
        if task is None or child_task_id not in task.child_task_ids:
            raise HTTPException(404, detail={"code": "not_found", "message": "child task not found"})
        values = payload.model_dump(mode="json")
        key = idem_key(request, idempotency_key)
        duplicate = store.check_idempotency(key, values)
        if duplicate:
            data = duplicate["event"]["data"]
            return envelope(data, version=data.get("version"))
        try:
            verification = verifier_service.verify(
                child_task_id,
                review_id,
                payload.verifier_agent_id,
                payload.evidence_ids,
                expected_review_version=payload.expected_review_version,
                idem=key,
                payload=values,
            )
        except Exception as error:
            handle_error(error)
        return envelope(verification.model_dump(mode="json"), version=verification.version)

    @app.get("/v1/tasks/{task_id}/children/{child_task_id}/reviews", response_model=ReviewListResponse)
    def get_reviews(task_id: str, child_task_id: str):
        task = store.tasks.get(task_id)
        if task is None or child_task_id not in task.child_task_ids:
            raise HTTPException(404, detail={"code": "not_found", "message": "child task not found"})
        reviews = [review.model_dump(mode="json") for review in store.reviews.values() if review.child_task_id == child_task_id]
        return envelope({"reviews": reviews}, version=task.version)

    @app.get("/v1/tasks/{task_id}/children/{child_task_id}/verifications", response_model=VerificationListResponse)
    def get_verifications(task_id: str, child_task_id: str):
        task = store.tasks.get(task_id)
        if task is None or child_task_id not in task.child_task_ids:
            raise HTTPException(404, detail={"code": "not_found", "message": "child task not found"})
        verifications = [record.model_dump(mode="json") for record in store.verifications.values() if record.child_task_id == child_task_id]
        return envelope({"verifications": verifications}, version=task.version)

    @app.get("/v1/tasks/{task_id}/usage", response_model=UsageResponse)
    def get_usage(task_id: str):
        if task_id not in store.tasks:
            raise HTTPException(404, detail={"code": "not_found", "message": "task not found"})
        report = build_usage_report(task_id, store.usage.get(task_id, []))
        return envelope(report.model_dump(mode="json"), version=store.tasks[task_id].version)

    @app.get("/v1/tasks/{task_id}/usage/export")
    def export_usage(task_id: str):
        if task_id not in store.tasks:
            raise HTTPException(404, detail={"code": "not_found", "message": "task not found"})
        report = build_usage_report(task_id, store.usage.get(task_id, []))
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=USAGE_EXPORT_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(usage_export_rows(report))
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="task-{task_id}-usage.csv"',
            },
        )

    @app.post("/v1/tasks/{task_id}/approvals", status_code=201, response_model=ApprovalResponse)
    async def request_approval(task_id: str, payload: RequestApprovalRequest, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        if task_id not in store.tasks:
            raise HTTPException(404, detail={"code": "not_found", "message": "task not found"})
        values = payload.model_dump(mode="json")
        key = idem_key(request, idempotency_key)
        duplicate = store.check_idempotency(key, values)
        if duplicate:
            data = duplicate["event"]["data"]["approval"]
            return envelope(data, version=data["version"])
        approval = Approval(
            id=new_id("approval"),
            action_summary=payload.action_summary,
            risk=payload.risk,
            expires_at=payload.expires_at,
            allowed_decisions=frozenset(payload.allowed_decisions),
        )
        event = {"type": "approval.requested", "data": {"task_id": task_id, "approval": approval.to_dict()}}
        store.commit(event, key=key, payload=values)
        store.approvals[approval.id] = approval; store.approval_task[approval.id] = task_id
        return envelope(approval.to_dict(), version=approval.version)

    @app.post("/v1/tasks/{task_id}/approvals/{approval_id}", status_code=202, response_model=ApprovalResponse)
    async def decide_approval(task_id: str, approval_id: str, payload: DecideApprovalRequest, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        approval = store.approvals.get(approval_id)
        if not approval or store.approval_task.get(approval_id) != task_id:
            raise HTTPException(404, detail={"code": "not_found", "message": "approval not found"})
        values = payload.model_dump(mode="json")
        key = idem_key(request, idempotency_key)
        duplicate = store.check_idempotency(key, values)
        if duplicate:
            data = duplicate["event"]["data"]["approval"]
            return envelope(data, version=data["version"])
        if payload.expected_version != approval.version:
            raise HTTPException(409, detail={"code": "version_conflict", "message": "approval version has changed"})
        try:
            decided = approval_service.decide(approval, payload.decision, reason=payload.reason, expected_version=payload.expected_version)
        except (KeyError, ValueError, ApprovalDecisionConflict, ApprovalDecisionNotAllowed, ApprovalExpired, ApprovalVersionConflict) as error:
            code = "version_conflict" if isinstance(error, ApprovalVersionConflict) else ("approval_conflict" if isinstance(error, ApprovalDecisionConflict) else "approval_denied")
            raise HTTPException(409, detail={"code": code, "message": str(error)}) from error
        event = {"type": f"approval.{decided.status.value}", "data": {"task_id": task_id, "approval": decided.to_dict()}}
        store.commit_frame([event], expected_versions={f"approval:{approval_id}": approval.version}, key=key, payload=values)
        store.approvals[approval_id] = decided
        return envelope(decided.to_dict(), version=decided.version)

    @app.get("/v1/tasks/{task_id}/approvals", response_model=ApprovalListResponse)
    def get_approvals(task_id: str):
        task = store.tasks.get(task_id)
        if task is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "task not found"})
        approvals = [ApprovalView(**approval.to_dict()).model_dump(mode="json") for approval_id, approval in store.approvals.items() if store.approval_task.get(approval_id) == task_id]
        return envelope({"approvals": approvals}, version=task.version)

    @app.get("/v1/tasks/{task_id}/approvals/{approval_id}", response_model=ApprovalResponse)
    def get_approval(task_id: str, approval_id: str):
        task = store.tasks.get(task_id)
        approval = store.approvals.get(approval_id)
        if task is None or approval is None or store.approval_task.get(approval_id) != task_id:
            raise HTTPException(404, detail={"code": "not_found", "message": "approval not found"})
        return envelope(ApprovalView(**approval.to_dict()).model_dump(mode="json"), version=approval.version)

    async def control_task(task_id: str, action: str, request_model: ControlTaskRequest, request: Request, idempotency_key: str | None):
        payload = request_model.model_dump(mode="json")
        expected = request_model.expected_version
        key = idem_key(request, idempotency_key)
        duplicate = store.check_idempotency(key, payload)
        try:
            if action == "execute" and duplicate is None:
                candidate = store.tasks.get(task_id)
                if candidate:
                    session = store.sessions[candidate.controller_session_id]
                    if session.orchestration_mode == "worker_only" and candidate.plan_id:
                        execution.validate_capacity(candidate, store.plans[candidate.plan_id])
            result = control.transition(task_id, action, expected, payload.get("reason"), key, payload)
            if action == "execute" and duplicate is None:
                task = store.tasks[task_id]
                session = store.sessions[task.controller_session_id]
                if session.orchestration_mode == "worker_only" and task.plan_id:
                    execution.enqueue_plan(task, store.plans[task.plan_id])
        except Exception as e:
            handle_error(e)
        return envelope(result.model_dump(mode="json"), version=result.version)

    @app.post("/v1/tasks/{task_id}/execute", status_code=202, response_model=TaskResponse)
    async def execute_task(task_id: str, payload: ControlTaskRequest, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        return await control_task(task_id, "execute", payload, request, idempotency_key)

    @app.post("/v1/tasks/{task_id}/pause", status_code=202, response_model=TaskResponse)
    async def pause_task(task_id: str, payload: ControlTaskRequest, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        return await control_task(task_id, "pause", payload, request, idempotency_key)

    @app.post("/v1/tasks/{task_id}/resume", status_code=202, response_model=TaskResponse)
    async def resume_task(task_id: str, payload: ControlTaskRequest, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        return await control_task(task_id, "resume", payload, request, idempotency_key)

    @app.post("/v1/tasks/{task_id}/cancel", status_code=202, response_model=TaskResponse)
    async def cancel_task(task_id: str, payload: ControlTaskRequest, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        return await control_task(task_id, "cancel", payload, request, idempotency_key)

    @app.post("/v1/tasks/{task_id}/plan", response_model=PlanResponse)
    async def submit_plan(task_id: str, payload: SubmitPlanRequest, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        try:
            proposal = WorkerPlanProposal.model_validate(payload.model_dump(mode="json"))
            plan, validation = service.plan(task_id, proposal, idem_key(request, idempotency_key))
        except Exception as e: handle_error(e)
        if not validation.valid:
            raise HTTPException(422, detail={"code": "invalid_plan", "message": "WorkerPlanProposal validation failed", "details": validation.model_dump(mode="json")})
        return envelope({"plan": plan.model_dump(mode="json"), "validation": validation.model_dump(mode="json")}, version=store.tasks[task_id].version)

    @app.post("/v1/advisory", status_code=202, response_model=AdvisoryResponse)
    async def advisory(payload: AdvisoryRequest, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        values = payload.model_dump(mode="json")
        record_shadow = values.pop("record_shadow")
        envelope_input = RoutingRequestEnvelope.model_validate(values)
        if record_shadow:
            advice, record = routing_service.analyze_and_shadow(envelope_input, values, key=idem_key(request, idempotency_key))
        else:
            advice, _ = routing_service.analyze(envelope_input)
            record = None
        return envelope({"advice": advice.model_dump(mode="json"), "shadow_id": record.id if record else None})

    @app.get("/v1/advisory/shadow", response_model=ShadowListResponse)
    def list_shadow():
        return envelope({"records": [record.model_dump(mode="json") for record in routing_service.list_shadow()]})

    @app.get("/v1/advisory/shadow/{record_id}", response_model=ShadowListResponse)
    def get_shadow(record_id: str):
        record = next((item for item in routing_service.list_shadow() if item.id == record_id), None)
        if not record:
            raise HTTPException(404, detail={"code": "not_found", "message": "shadow record not found"})
        return envelope({"records": [record.model_dump(mode="json")]})

    @app.get("/v1/tasks/{task_id}/events")
    async def events(
        task_id: str,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        follow: bool = False,
        timeout_seconds: float = 15.0,
    ):
        if task_id not in store.tasks:
            raise HTTPException(404, detail={"code": "not_found", "message": "task not found"})
        try:
            after = parse_last_event_id(last_event_id)
        except EventStreamError as error:
            raise HTTPException(400, detail={"code": "invalid_last_event_id", "message": str(error)}) from error
        def belongs(event: dict[str, Any]) -> bool:
            data = event.get("data", {})
            return data.get("id") == task_id or data.get("task_id") == task_id or data.get("parent_task_id") == task_id

        raw_events = []
        for event in journal.events(after):
            if belongs(event):
                data = event.get("data", {})
                raw_events.append({"id": f"evt_{event['sequence']}", "sequence": event["sequence"], "type": event["type"], "data": data})
        replay = deduplicate_events(raw_events)

        async def stream():
            for event in replay:
                yield format_sse_frame(event)
            if not follow:
                return
            if timeout_seconds <= 0:
                return
            deadline = time.monotonic() + min(timeout_seconds, 30.0)
            cursor = max((event["sequence"] for event in replay), default=after)
            seen = {event["sequence"] for event in replay}
            while time.monotonic() < deadline:
                emitted = False
                for event in journal.events(cursor):
                    cursor = max(cursor, event["sequence"])
                    if event["sequence"] in seen or not belongs(event):
                        continue
                    seen.add(event["sequence"])
                    emitted = True
                    data = event.get("data", {})
                    yield format_sse_frame({"id": f"evt_{event['sequence']}", "sequence": event["sequence"], "type": event["type"], "data": data})
                if not emitted:
                    await asyncio.sleep(0.05)
        return StreamingResponse(stream(), media_type="text/event-stream")

    return app
