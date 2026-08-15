from pathlib import Path

from fastapi.testclient import TestClient

from valueroute.api.app import create_app


def create_task(client: TestClient):
    session = client.post("/v1/controller-sessions", headers={"Idempotency-Key": "s"}, json={"tenant_id": "t", "host_session_id": "h", "orchestration_mode": "off"}).json()["data"]
    client.post(f"/v1/controller-sessions/{session['id']}/epochs", headers={"Idempotency-Key": "e"}, json={"expected_version": 1, "provider_id": "openai", "model_id": "test", "reasoning_effort": "low"})
    task = client.post("/v1/tasks", headers={"Idempotency-Key": "t"}, json={"controller_session_id": session["id"], "request_type": "new_task", "goal": "run", "acceptance_contract": [{"id": "a", "description": "pass"}], "data_classification": "internal", "workspace": {"canonical_uri": "workspace://x", "base_revision": "r1"}})
    return task.json()["data"]


def test_execute_pause_resume_cancel_transitions(tmp_path: Path):
    client = TestClient(create_app(tmp_path))
    task = create_task(client)
    running = client.post(f"/v1/tasks/{task['id']}/execute", headers={"Idempotency-Key": "x"}, json={"expected_version": 1}).json()["data"]
    assert running["status"] == "running"
    assert running["latest_checkpoint_id"]
    repeated = client.post(f"/v1/tasks/{task['id']}/execute", headers={"Idempotency-Key": "x"}, json={"expected_version": 1}).json()["data"]
    assert repeated["id"] == running["id"] and repeated["version"] == running["version"]
    paused = client.post(f"/v1/tasks/{task['id']}/pause", headers={"Idempotency-Key": "p"}, json={"expected_version": 4}).json()["data"]
    assert paused["status"] == "paused"
    assert paused["latest_checkpoint_id"]
    resumed = client.post(f"/v1/tasks/{task['id']}/resume", headers={"Idempotency-Key": "r"}, json={"expected_version": 6}).json()["data"]
    assert resumed["status"] == "running"
    cancelled = client.post(f"/v1/tasks/{task['id']}/cancel", headers={"Idempotency-Key": "c"}, json={"expected_version": 8}).json()["data"]
    assert cancelled["status"] == "cancelled"


def test_execute_requires_controller_epoch(tmp_path: Path):
    client = TestClient(create_app(tmp_path))
    session = client.post("/v1/controller-sessions", headers={"Idempotency-Key": "s"}, json={"tenant_id": "t", "host_session_id": "h", "orchestration_mode": "off"}).json()["data"]
    task = client.post("/v1/tasks", headers={"Idempotency-Key": "t"}, json={"controller_session_id": session["id"], "request_type": "new_task", "goal": "run", "acceptance_contract": [{"id": "a", "description": "pass"}], "data_classification": "internal", "workspace": {"canonical_uri": "workspace://x", "base_revision": "r1"}}).json()["data"]
    response = client.post(f"/v1/tasks/{task['id']}/execute", headers={"Idempotency-Key": "x"}, json={"expected_version": 1})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "controller_not_registered"
