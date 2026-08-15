import pytest

from valueroute.application.service import DomainError
from valueroute.domain.models import ResourceRegion
from valueroute.ownership.boundaries import ChildTaskBoundary, OwnershipBoundaryService


def region(path: str, revision: str = "r1") -> ResourceRegion:
    return ResourceRegion(resource_kind="directory", resource_id="workspace", selector_type="path_prefix", selector_value=path, base_revision=revision)


def service() -> OwnershipBoundaryService:
    ownership = OwnershipBoundaryService()
    ownership.register(ChildTaskBoundary(id="ct_backend", parent_task_id="pt_1", objective="repair backend", write_regions=(region("backend"),)))
    return ownership


def test_child_task_has_exactly_one_active_owner():
    ownership = service()
    first = ownership.assign("ct_backend", "owner_a", [region("backend/api")])

    assert first.owner_agent_id == "owner_a"
    with pytest.raises(DomainError, match="active owner"):
        ownership.assign("ct_backend", "owner_b", [region("backend/api")])


def test_transfer_requires_explicit_release_of_old_owner():
    ownership = service()
    ownership.assign("ct_backend", "owner_a", [region("backend")])

    with pytest.raises(DomainError, match="release"):
        ownership.transfer("ct_backend", "owner_b", [region("backend")])
    released = ownership.release("ct_backend", "owner_a")
    successor = ownership.transfer("ct_backend", "owner_b", [region("backend")])

    assert released.status == "released"
    assert successor.owner_agent_id == "owner_b"
    assert successor.status == "active"


def test_unknown_or_out_of_boundary_assignment_is_rejected():
    ownership = service()

    with pytest.raises(DomainError, match="unknown or out-of-bound"):
        ownership.assign("ct_backend", "owner_a", [region("frontend")])
    with pytest.raises(DomainError, match="not found"):
        ownership.assign("ct_unknown", "owner_a", [region("backend")])


def test_owner_cannot_write_an_unknown_or_out_of_scope_region():
    ownership = service()
    ownership.assign("ct_backend", "owner_a", [region("backend/api")])

    ownership.assert_write_allowed("ct_backend", "owner_a", region("backend/api/routes"))
    with pytest.raises(DomainError, match="unknown or outside"):
        ownership.assert_write_allowed("ct_backend", "owner_a", region("backend/worker"))
    with pytest.raises(DomainError, match="not assigned"):
        ownership.assert_write_allowed("ct_backend", "owner_b", region("backend/api"))
