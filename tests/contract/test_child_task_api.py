from pathlib import Path

from fastapi.testclient import TestClient

from valueroute.api.app import create_app
from valueroute.domain.models import ResourceRegion


def test_plan_creates_persisted_child_boundaries(tmp_path: Path):
    app = create_app(tmp_path); client = TestClient(app)
    session = client.post("/v1/controller-sessions", headers={"Idempotency-Key": "s"}, json={"tenant_id": "t", "host_session_id": "h", "orchestration_mode": "worker_only"}).json()["data"]
    task = client.post("/v1/tasks", headers={"Idempotency-Key": "t"}, json={"controller_session_id": session["id"], "request_type": "new_task", "goal": "split", "acceptance_contract": [{"id": "a", "description": "pass"}], "data_classification": "internal", "workspace": {"canonical_uri": "workspace://x", "base_revision": "r1"}}).json()["data"]
    plan = {"expected_parent_version": 1, "children": [{"client_ref": "backend", "objective": "fix backend", "write_regions": [{"resource_kind": "file", "resource_id": "app.py", "selector_type": "whole_resource", "selector_value": "", "base_revision": "r1"}], "acceptance_contract": ["tests"]}], "integration_order": ["backend"]}
    response = client.post(f"/v1/tasks/{task['id']}/plan", headers={"Idempotency-Key": "p"}, json=plan)
    assert response.status_code == 200
    children = client.get(f"/v1/tasks/{task['id']}/children").json()["data"]["children"]
    assert len(children) == 1 and children[0]["parent_task_id"] == task["id"]
    app.state.store.journal.close()

    restarted = TestClient(create_app(tmp_path))
    restored = restarted.get(f"/v1/tasks/{task['id']}/children").json()["data"]["children"]
    assert restored[0]["id"] == children[0]["id"]
    restarted.app.state.store.journal.close()


def test_owner_assignment_updates_store_and_replays(tmp_path: Path):
    first_app = create_app(tmp_path)
    client = TestClient(first_app)
    session = client.post("/v1/controller-sessions", headers={"Idempotency-Key": "s"}, json={"tenant_id": "t", "host_session_id": "h", "orchestration_mode": "worker_only"}).json()["data"]
    task = client.post("/v1/tasks", headers={"Idempotency-Key": "t"}, json={"controller_session_id": session["id"], "request_type": "new_task", "goal": "assign", "acceptance_contract": [{"id": "a", "description": "pass"}], "data_classification": "internal", "workspace": {"canonical_uri": "workspace://x", "base_revision": "r1"}}).json()["data"]
    plan = {"expected_parent_version": 1, "children": [{"client_ref": "backend", "objective": "backend", "write_regions": [{"resource_kind": "file", "resource_id": "repo", "selector_type": "path_prefix", "selector_value": "src", "base_revision": "r1"}], "acceptance_contract": ["pass"]}], "integration_order": ["backend"]}
    client.post(f"/v1/tasks/{task['id']}/plan", headers={"Idempotency-Key": "p"}, json=plan)
    child = client.get(f"/v1/tasks/{task['id']}/children").json()["data"]["children"][0]
    assigned = first_app.state.ownership.assign(child["id"], "worker-a", [ResourceRegion.model_validate(child["write_regions"][0])])
    assert first_app.state.store.assignments[child["id"]].owner_agent_id == "worker-a"
    first_app.state.store.journal.close()

    restarted = TestClient(create_app(tmp_path))
    assert restarted.app.state.store.assignments[child["id"]].owner_agent_id == assigned.owner_agent_id
    restarted.app.state.store.journal.close()
