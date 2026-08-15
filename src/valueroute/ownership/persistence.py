"""Event-backed persistence for child-task ownership boundaries.

The adapter deliberately depends only on two callables so callers can use a
journal, a database outbox, or a test double without coupling ownership to the
existing Store or API surface.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from valueroute.domain.errors import DomainError
from valueroute.domain.models import ResourceRegion
from valueroute.ownership.boundaries import ChildTaskBoundary, OwnerAssignment, OwnershipBoundaryService


AppendEvent = Callable[[dict[str, Any]], None]
ListEvents = Callable[[], Iterable[Mapping[str, Any]]]


BOUNDARY_REGISTERED = "ownership.boundary_registered"
OWNER_ASSIGNED = "ownership.owner_assigned"
OWNER_RELEASED = "ownership.owner_released"
OWNER_TRANSFERRED = "ownership.owner_transferred"


class PersistentOwnershipBoundaryService(OwnershipBoundaryService):
    """An ownership boundary service whose state is rebuilt from its events.

    ``append_event`` receives one ``{"type": ..., "data": ...}`` record.
    ``list_events`` returns records in durable order and may also include
    unrelated application events; those are ignored during recovery.
    """

    def __init__(self, append_event: AppendEvent, list_events: ListEvents) -> None:
        super().__init__()
        self._append_event = append_event
        self._list_events = list_events
        self._rebuild()

    def register(self, boundary: ChildTaskBoundary) -> ChildTaskBoundary:
        result = super().register(boundary)
        self._append(BOUNDARY_REGISTERED, boundary.model_dump(mode="json"))
        return result

    def assign(
        self,
        child_task_id: str,
        owner_agent_id: str,
        write_regions: tuple[ResourceRegion, ...] | list[ResourceRegion],
    ) -> OwnerAssignment:
        assignment = super().assign(child_task_id, owner_agent_id, write_regions)
        self._append(OWNER_ASSIGNED, assignment.model_dump(mode="json"))
        return assignment

    def release(self, child_task_id: str, owner_agent_id: str) -> OwnerAssignment:
        assignment = super().release(child_task_id, owner_agent_id)
        self._append(OWNER_RELEASED, assignment.model_dump(mode="json"))
        return assignment

    def transfer(
        self,
        child_task_id: str,
        new_owner_agent_id: str,
        write_regions: tuple[ResourceRegion, ...] | list[ResourceRegion],
    ) -> OwnerAssignment:
        current = self._assignments.get(child_task_id)
        if current is not None and current.status == "active":
            raise DomainError("transfer_requires_release", "release the active owner before assigning a successor")
        assignment = OwnershipBoundaryService.assign(self, child_task_id, new_owner_agent_id, write_regions)
        self._append(OWNER_TRANSFERRED, assignment.model_dump(mode="json"))
        return assignment

    def _append(self, event_type: str, data: dict[str, Any]) -> None:
        self._append_event({"type": event_type, "data": data})

    def _rebuild(self) -> None:
        for event in self._list_events():
            event_type = event.get("type")
            data = event.get("data")
            if event_type not in {BOUNDARY_REGISTERED, OWNER_ASSIGNED, OWNER_RELEASED, OWNER_TRANSFERRED}:
                continue
            if not isinstance(data, Mapping):
                raise DomainError("invalid_ownership_event", "ownership event data must be an object")
            if event_type == BOUNDARY_REGISTERED:
                boundary = ChildTaskBoundary.model_validate(data)
                if boundary.id in self._boundaries:
                    raise DomainError("ownership_event_conflict", "duplicate child task boundary in event stream")
                self._boundaries[boundary.id] = boundary
                continue

            assignment = OwnerAssignment.model_validate(data)
            self._require_boundary(assignment.child_task_id)
            current = self._assignments.get(assignment.child_task_id)
            if event_type in {OWNER_ASSIGNED, OWNER_TRANSFERRED}:
                if assignment.status != "active":
                    raise DomainError("invalid_ownership_event", "assignment event must contain an active owner")
                if current is not None and current.status == "active":
                    raise DomainError("ownership_event_conflict", "event stream has more than one active owner")
                self._assignments[assignment.child_task_id] = assignment
            else:
                if assignment.status != "released":
                    raise DomainError("invalid_ownership_event", "release event must contain a released owner")
                if current is None or current.status != "active" or current.owner_agent_id != assignment.owner_agent_id:
                    raise DomainError("ownership_event_conflict", "release event has no matching active owner")
                self._assignments[assignment.child_task_id] = assignment


# A descriptive alias for integrations that name services by their role.
OwnershipBoundaryPersistenceService = PersistentOwnershipBoundaryService
