from __future__ import annotations

import hashlib
import json
from typing import Any

from valueroute.domain.models import *
from valueroute.domain.state_machine import transition_task
from valueroute.storage.interfaces import StateStore
from valueroute.domain.errors import DomainError
from valueroute.ownership.boundaries import ChildTaskBoundary
from valueroute.ownership.persistence import PersistentOwnershipBoundaryService


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


class Service:
    def __init__(self, store: StateStore, ownership: PersistentOwnershipBoundaryService | None = None):
        self.store = store
        self.ownership = ownership

    def create_session(self, payload: dict[str, Any], idem: tuple[str, str, str] | None) -> ControllerSession:
        if (old := self.store.check_idempotency(idem, payload)):
            return ControllerSession.model_validate(old["event"]["data"])
        session = ControllerSession(id=new_id("cs"), **payload)
        self.store.sessions[session.id] = session
        self.store.commit({"type": "session.created", "data": session.model_dump(mode="json")}, key=idem, payload=payload)
        return session

    def register_epoch(self, session_id: str, payload: dict[str, Any], expected_version: int, idem: tuple[str, str, str] | None) -> ControllerEpoch:
        session = self.store.sessions.get(session_id)
        if not session: raise DomainError("not_found", "controller session not found", 404)
        self.store.require_version(session.version, expected_version)
        if session.active_controller_epoch_id:
            raise DomainError("controller_already_registered", "首版不允许切换活动主控")
        epoch = ControllerEpoch(id=new_id("ce"), controller_session_id=session_id, **payload)
        self.store.epochs[epoch.id] = epoch
        self.store.sessions[session.id] = session.model_copy(update={"active_controller_epoch_id": epoch.id, "version": session.version + 1})
        self.store.commit({"type": "session.epoch_registered", "data": epoch.model_dump(mode="json")}, key=idem, payload=payload | {"expected_version": expected_version})
        return epoch

    def create_task(self, payload: dict[str, Any], idem: tuple[str, str, str] | None) -> ParentTask:
        if (old := self.store.check_idempotency(idem, payload)):
            return ParentTask.model_validate(old["event"]["data"])
        session = self.store.sessions.get(payload["controller_session_id"])
        if not session: raise DomainError("not_found", "controller session not found", 404)
        task = ParentTask(id=new_id("pt"), **payload)
        self.store.tasks[task.id] = task
        self.store.commit({"type": "task.created", "data": task.model_dump(mode="json")}, key=idem, payload=payload)
        return task

    def plan(self, task_id: str, proposal: WorkerPlanProposal, idem: tuple[str, str, str] | None) -> tuple[WorkerPlan | None, PlanValidationResult]:
        task = self.store.tasks.get(task_id)
        if not task: raise DomainError("not_found", "task not found", 404)
        if (old := self.store.check_idempotency(idem, proposal.model_dump(mode="json"))):
            plan = WorkerPlan.model_validate(old["event"]["data"])
            return plan, PlanValidationResult(valid=True, proposal_hash=plan.proposal_hash, normalized_plan_id=plan.id)
        if task.version != proposal.expected_parent_version:
            raise DomainError("version_conflict", "task version has changed")
        issues: list[PlanValidationIssue] = []
        refs = {c.client_ref for c in proposal.children}
        if len(refs) != len(proposal.children): issues.append(PlanValidationIssue(severity="error", code="duplicate_client_ref", path="/children", message="client_ref must be unique"))
        if set(proposal.integration_order) != refs: issues.append(PlanValidationIssue(severity="error", code="integration_order_incomplete", path="/integration_order", message="integration_order must cover every child"))
        for i, child in enumerate(proposal.children):
            unknown = set(child.depends_on) - refs
            if unknown: issues.append(PlanValidationIssue(severity="error", code="unknown_dependency", path=f"/children/{i}/depends_on", message=f"unknown dependencies: {sorted(unknown)}"))
            for j, region in enumerate(child.write_regions):
                if not {"resource_kind", "resource_id", "selector_type", "selector_value", "base_revision"} <= region.keys():
                    issues.append(PlanValidationIssue(severity="error", code="invalid_region", path=f"/children/{i}/write_regions/{j}", message="region must include resource and selector fields"))
        if any(set(c.depends_on) & {c.client_ref} for c in proposal.children): issues.append(PlanValidationIssue(severity="error", code="dependency_cycle", path="/children", message="a child cannot depend on itself"))
        result = PlanValidationResult(valid=not any(x.severity == "error" for x in issues), proposal_hash=digest(proposal.model_dump(mode="json")), issues=issues)
        if not result.valid: return None, result
        plan = WorkerPlan(id=new_id("wp"), parent_task_id=task_id, proposal_hash=result.proposal_hash, children=proposal.children, integration_order=proposal.integration_order)
        result = result.model_copy(update={"normalized_plan_id": plan.id})
        child_ids: list[str] = []
        for child in proposal.children:
            regions = tuple(ResourceRegion.model_validate(region) for region in child.write_regions)
            child_boundary = ChildTaskBoundary(id=new_id("ct"), parent_task_id=task_id, objective=child.objective, write_regions=regions)
            if self.ownership is not None:
                self.ownership.register(child_boundary)
            self.store.children[child_boundary.id] = child_boundary
            self.store.child_ref_ids[(task_id, child.client_ref)] = child_boundary.id
            child_ids.append(child_boundary.id)
            self.store.commit({"type": "child_task.created", "data": child_boundary.model_dump(mode="json")})
        updated = transition_task(task, TaskStatus.planned, task.version).model_copy(update={"plan_id": plan.id, "child_task_ids": child_ids})
        self.store.plans[plan.id] = plan; self.store.tasks[task_id] = updated
        self.store.commit({"type": "plan.committed", "data": plan.model_dump(mode="json")}, key=idem, payload=proposal.model_dump(mode="json"))
        self.store.commit({"type": "task.updated", "data": updated.model_dump(mode="json")})
        return plan, result

    def acquire_lease(self, lease: WriterLease) -> WriterLease:
        for current in self.store.leases.values():
            if current.status != "active": continue
            if overlaps(current.region, lease.region): raise DomainError("lease_overlap", "resource regions overlap")
        self.store.leases[lease.id] = lease
        self.store.commit({"type": "lease.acquired", "data": lease.model_dump(mode="json")})
        return lease


def overlaps(a: ResourceRegion, b: ResourceRegion) -> bool:
    if (a.resource_kind, a.resource_id) != (b.resource_kind, b.resource_id): return False
    if a.base_revision != b.base_revision: return True
    if a.selector_type == "whole_resource" or b.selector_type == "whole_resource": return True
    if a.selector_type == b.selector_type == "symbol":
        return a.selector_value == b.selector_value
    if a.selector_type == b.selector_type == "path_prefix":
        left, right = str(a.selector_value).rstrip("/"), str(b.selector_value).rstrip("/")
        return left == right or left.startswith(right + "/") or right.startswith(left + "/")
    if a.selector_type == b.selector_type == "row_keys":
        left, right = _value_set(a.selector_value), _value_set(b.selector_value)
        return left is None or right is None or bool(left & right)
    if a.selector_type == b.selector_type == "partition":
        return a.selector_value == b.selector_value
    if a.selector_type == b.selector_type == "key_range":
        return _ranges_overlap(a.selector_value, b.selector_value)
    if a.selector_type == b.selector_type == "json_pointer":
        left, right = str(a.selector_value).rstrip("/"), str(b.selector_value).rstrip("/")
        return left == right or left.startswith(right + "/") or right.startswith(left + "/")
    if a.selector_type == b.selector_type == "provider_subresource":
        return a.selector_value == b.selector_value
    # Unknown overlap semantics are conservatively treated as conflicting.
    return True


def _value_set(value: Any) -> set[str] | None:
    if isinstance(value, (list, tuple, set, frozenset)):
        return {repr(item) for item in value}
    if isinstance(value, dict) and "keys" in value and isinstance(value["keys"], (list, tuple, set, frozenset)):
        return {repr(item) for item in value["keys"]}
    return None


def _ranges_overlap(left: Any, right: Any) -> bool:
    def bounds(value: Any) -> tuple[float, float] | None:
        if isinstance(value, dict) and "start" in value and "end" in value:
            try:
                return float(value["start"]), float(value["end"])
            except (TypeError, ValueError):
                return None
        if isinstance(value, (list, tuple)) and len(value) == 2:
            try:
                return float(value[0]), float(value[1])
            except (TypeError, ValueError):
                return None
        return None

    a_bounds, b_bounds = bounds(left), bounds(right)
    if a_bounds is None or b_bounds is None:
        return True
    return max(a_bounds[0], b_bounds[0]) <= min(a_bounds[1], b_bounds[1])
