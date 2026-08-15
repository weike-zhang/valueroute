import pytest

from valueroute.application.service import DomainError
from valueroute.domain.models import ResourceRegion
from valueroute.ownership.boundaries import ChildTaskBoundary
from valueroute.ownership.persistence import (
    BOUNDARY_REGISTERED,
    OWNER_ASSIGNED,
    OWNER_RELEASED,
    OWNER_TRANSFERRED,
    PersistentOwnershipBoundaryService,
)


def region(path: str, revision: str = "r1") -> ResourceRegion:
    return ResourceRegion(resource_kind="directory", resource_id="workspace", selector_type="path_prefix", selector_value=path, base_revision=revision)


def boundary() -> ChildTaskBoundary:
    return ChildTaskBoundary(id="ct_backend", parent_task_id="pt_1", objective="repair backend", write_regions=(region("backend"),))


def test_events_rebuild_boundary_and_active_owner_after_restart():
    events: list[dict] = []
    ownership = PersistentOwnershipBoundaryService(events.append, lambda: events)
    ownership.register(boundary())
    ownership.assign("ct_backend", "owner_a", [region("backend/api")])

    restarted = PersistentOwnershipBoundaryService(events.append, lambda: events)

    restarted.assert_write_allowed("ct_backend", "owner_a", region("backend/api/routes"))
    with pytest.raises(DomainError, match="active owner"):
        restarted.assign("ct_backend", "owner_b", [region("backend/api")])
    assert [event["type"] for event in events] == [BOUNDARY_REGISTERED, OWNER_ASSIGNED]


def test_release_and_transfer_are_recorded_and_replay_to_successor():
    events: list[dict] = []
    ownership = PersistentOwnershipBoundaryService(events.append, lambda: events)
    ownership.register(boundary())
    ownership.assign("ct_backend", "owner_a", [region("backend")])
    ownership.release("ct_backend", "owner_a")
    successor = ownership.transfer("ct_backend", "owner_b", [region("backend/api")])

    restarted = PersistentOwnershipBoundaryService(events.append, lambda: events)

    assert successor.owner_agent_id == "owner_b"
    restarted.assert_write_allowed("ct_backend", "owner_b", region("backend/api/routes"))
    assert [event["type"] for event in events] == [BOUNDARY_REGISTERED, OWNER_ASSIGNED, OWNER_RELEASED, OWNER_TRANSFERRED]


def test_persisted_service_fails_closed_for_out_of_scope_writes():
    events: list[dict] = []
    ownership = PersistentOwnershipBoundaryService(events.append, lambda: events)
    ownership.register(boundary())
    ownership.assign("ct_backend", "owner_a", [region("backend/api")])

    with pytest.raises(DomainError, match="unknown or outside"):
        ownership.assert_write_allowed("ct_backend", "owner_a", region("backend/worker"))
    with pytest.raises(DomainError, match="not assigned"):
        ownership.assert_write_allowed("ct_backend", "owner_b", region("backend/api"))
