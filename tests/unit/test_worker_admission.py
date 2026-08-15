import pytest

from valueroute.domain.errors import DomainError
from valueroute.execution.admission import MAX_DELEGATION_DEPTH, MAX_WORKERS, WorkerAdmission


def test_controller_can_admit_at_the_contract_boundary():
    WorkerAdmission(requested_workers=MAX_WORKERS).validate()


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"actor_role": "worker", "requested_workers": 1}, "worker_spawn_forbidden"),
        ({"parent_depth": MAX_DELEGATION_DEPTH, "requested_workers": 1}, "worker_depth_exceeded"),
        ({"requested_workers": MAX_WORKERS + 1}, "worker_limit_exceeded"),
    ],
)
def test_worker_boundary_rejects_invalid_admission(kwargs, code):
    with pytest.raises(DomainError) as error:
        WorkerAdmission(**kwargs).validate()
    assert error.value.code == code


def test_negative_depth_is_rejected_before_any_queue_side_effect():
    with pytest.raises(DomainError, match="negative"):
        WorkerAdmission(parent_depth=-1, requested_workers=1).validate()
