"""Strict ChildTask ownership and write-boundary enforcement.

This module deliberately has no dependency on the existing Store: ownership is
an application boundary that can be persisted by a later adapter without
changing the domain or storage models already exposed by v0.0.1.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator, model_validator

from valueroute.domain.errors import DomainError
from valueroute.domain.models import ResourceRegion, StrictModel, now


class ChildTaskBoundary(StrictModel):
    """An independently executable child goal with an explicit write scope."""

    id: str = Field(min_length=1, max_length=200)
    parent_task_id: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=4000)
    write_regions: tuple[ResourceRegion, ...] = Field(default_factory=tuple)
    version: int = Field(default=1, ge=1)

    @field_validator("write_regions")
    @classmethod
    def write_regions_must_be_unique(cls, regions: tuple[ResourceRegion, ...]) -> tuple[ResourceRegion, ...]:
        identities = {(region.resource_kind, region.resource_id, region.selector_type, repr(region.selector_value), region.base_revision) for region in regions}
        if len(identities) != len(regions):
            raise ValueError("duplicate write region")
        return regions


class OwnerAssignment(StrictModel):
    """The sole Owner binding for one ChildTaskBoundary at a point in time."""

    child_task_id: str = Field(min_length=1, max_length=200)
    owner_agent_id: str = Field(min_length=1, max_length=200)
    write_regions: tuple[ResourceRegion, ...] = Field(min_length=1)
    version: int = Field(default=1, ge=1)
    status: str = "active"
    assigned_at: datetime = Field(default_factory=now)
    released_at: datetime | None = None

    @model_validator(mode="after")
    def valid_lifecycle(self) -> "OwnerAssignment":
        if self.status not in {"active", "released"}:
            raise ValueError("assignment status must be active or released")
        if self.status == "active" and self.released_at is not None:
            raise ValueError("active assignment cannot have released_at")
        if self.status == "released" and self.released_at is None:
            raise ValueError("released assignment requires released_at")
        return self


def _covers(allowed: ResourceRegion, requested: ResourceRegion) -> bool:
    """Return true only when containment is deterministic and version-safe."""
    if (allowed.resource_kind, allowed.resource_id, allowed.base_revision) != (
        requested.resource_kind,
        requested.resource_id,
        requested.base_revision,
    ):
        return False
    if allowed.selector_type == "whole_resource":
        return True
    if allowed.selector_type == requested.selector_type == "path_prefix":
        prefix = str(allowed.selector_value).rstrip("/")
        path = str(requested.selector_value).rstrip("/")
        return path == prefix or path.startswith(prefix + "/")
    return allowed.selector_type == requested.selector_type and allowed.selector_value == requested.selector_value


class OwnershipBoundaryService:
    """Fail-closed owner registry for declared child-task write regions."""

    def __init__(self) -> None:
        self._boundaries: dict[str, ChildTaskBoundary] = {}
        self._assignments: dict[str, OwnerAssignment] = {}

    def register(self, boundary: ChildTaskBoundary) -> ChildTaskBoundary:
        if boundary.id in self._boundaries:
            raise DomainError("boundary_conflict", "child task boundary already registered")
        self._boundaries[boundary.id] = boundary
        return boundary

    def assign(self, child_task_id: str, owner_agent_id: str, write_regions: tuple[ResourceRegion, ...] | list[ResourceRegion]) -> OwnerAssignment:
        boundary = self._require_boundary(child_task_id)
        current = self._assignments.get(child_task_id)
        if current is not None and current.status == "active":
            raise DomainError("owner_conflict", "child task already has an active owner")
        regions = tuple(write_regions)
        if not regions:
            raise DomainError("unknown_write_region", "owner assignment requires declared write regions")
        if any(not any(_covers(allowed, region) for allowed in boundary.write_regions) for region in regions):
            raise DomainError("write_scope_violation", "owner assignment includes an unknown or out-of-bound region")
        assignment = OwnerAssignment(child_task_id=child_task_id, owner_agent_id=owner_agent_id, write_regions=regions)
        self._assignments[child_task_id] = assignment
        return assignment

    def release(self, child_task_id: str, owner_agent_id: str) -> OwnerAssignment:
        assignment = self._require_active_assignment(child_task_id)
        if assignment.owner_agent_id != owner_agent_id:
            raise DomainError("owner_mismatch", "only the active owner can release this assignment")
        released = assignment.model_copy(update={"status": "released", "released_at": now(), "version": assignment.version + 1})
        self._assignments[child_task_id] = released
        return released

    def transfer(self, child_task_id: str, new_owner_agent_id: str, write_regions: tuple[ResourceRegion, ...] | list[ResourceRegion]) -> OwnerAssignment:
        """Create a successor only after the caller has released the old owner."""
        current = self._assignments.get(child_task_id)
        if current is not None and current.status == "active":
            raise DomainError("transfer_requires_release", "release the active owner before assigning a successor")
        return self.assign(child_task_id, new_owner_agent_id, write_regions)

    def assert_write_allowed(self, child_task_id: str, owner_agent_id: str, region: ResourceRegion) -> None:
        self._require_boundary(child_task_id)
        assignment = self._require_active_assignment(child_task_id)
        if assignment.owner_agent_id != owner_agent_id:
            raise DomainError("owner_mismatch", "owner is not assigned to this child task")
        if not any(_covers(allowed, region) for allowed in assignment.write_regions):
            raise DomainError("write_scope_violation", "write region is unknown or outside the owner's assignment")

    def _require_boundary(self, child_task_id: str) -> ChildTaskBoundary:
        boundary = self._boundaries.get(child_task_id)
        if boundary is None:
            raise DomainError("not_found", "child task boundary not found", 404)
        return boundary

    def _require_active_assignment(self, child_task_id: str) -> OwnerAssignment:
        assignment = self._assignments.get(child_task_id)
        if assignment is None or assignment.status != "active":
            raise DomainError("owner_not_assigned", "child task has no active owner")
        return assignment
