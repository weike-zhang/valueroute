from pathlib import Path

from fastapi.testclient import TestClient

from valueroute.api.app import create_app
from valueroute.domain.models import WorkerAttempt, WorkerAttemptStatus


def _seed(client: TestClient, *, session_provider: str = "openai"):
    session = client.post("/v1/controller-sessions", headers={"Idempotency-Key": "s"}, json={
        "tenant_id": "t", "host_session_id": "h", "orchestration_mode": "worker_only",
    }).json()["data"]
    client.post(f"/v1/controller-sessions/{session['id']}/epochs", headers={"Idempotency-Key": "e"}, json={
        "expected_version": 1, "provider_id": session_provider, "model_id": "gpt-a", "reasoning_effort": "low",
    })
    task = client.post("/v1/tasks", headers={"Idempotency-Key": "t"}, json={
        "controller_session_id": session["id"], "request_type": "new_task", "goal": "do work",
        "acceptance_contract": [{"id": "a", "description": "done"}], "data_classification": "internal",
        "workspace": {"canonical_uri": "workspace://x", "base_revision": "r1"},
    }).json()["data"]
    client.post(f"/v1/tasks/{task['id']}/plan", headers={"Idempotency-Key": "p"}, json={
        "expected_parent_version": 1,
        "children": [{"client_ref": "c1", "objective": "objective", "write_regions": [], "acceptance_contract": ["pass"]}],
        "integration_order": ["c1"],
    })
    children = [child for child in client.app.state.store.children.values() if child.parent_task_id == task["id"]]
    child_id = children[0].id
    attempt = WorkerAttempt(id="wa_1", worker_session_id="ws_1", child_task_id=child_id, status=WorkerAttemptStatus.claimed)
    client.app.state.store.attempts[attempt.id] = attempt
    client.app.state.store.commit({"type": "worker.claimed", "data": attempt.model_dump(mode="json")})
    return session, task, child_id


def test_handoff_api_records_egress_and_repaints_attempt(tmp_path: Path):
    client = TestClient(create_app(tmp_path))
    _, task, child_id = _seed(client)

    response = client.post(f"/v1/tasks/{task['id']}/handoff", headers={"Idempotency-Key": "h1"}, json={
        "child_task_id": child_id,
        "target_provider": "anthropic",
        "target_model": "claude-x",
        "fields": ["task_id", "goal"],
        "data_classification": "internal",
    })
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["target_provider"] == "anthropic"
    assert data["mode"] == "read_only_handoff"

    egress = client.get(f"/v1/egress?task_id={task['id']}")
    assert egress.status_code == 200
    records = egress.json()["data"]["records"]
    assert len(records) == 1
    assert records[0]["target_provider"] == "anthropic"


def test_handoff_api_is_idempotent(tmp_path: Path):
    client = TestClient(create_app(tmp_path))
    _, task, child_id = _seed(client)
    payload = {
        "child_task_id": child_id,
        "target_provider": "anthropic",
        "target_model": "claude-x",
        "fields": ["task_id"],
        "data_classification": "internal",
    }
    first = client.post(f"/v1/tasks/{task['id']}/handoff", headers={"Idempotency-Key": "h1"}, json=payload).json()["data"]
    second = client.post(f"/v1/tasks/{task['id']}/handoff", headers={"Idempotency-Key": "h1"}, json=payload).json()["data"]
    assert second["egress_id"] == first["egress_id"]


def test_handoff_api_denied_for_confidential(tmp_path: Path):
    client = TestClient(create_app(tmp_path))
    _, task, child_id = _seed(client)
    response = client.post(f"/v1/tasks/{task['id']}/handoff", headers={"Idempotency-Key": "h1"}, json={
        "child_task_id": child_id,
        "target_provider": "anthropic",
        "target_model": "claude-x",
        "fields": ["task_id"],
        "data_classification": "confidential",
    })
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "egress_denied"


def test_egress_list_filters_by_target_provider(tmp_path: Path):
    client = TestClient(create_app(tmp_path))
    _, task, child_id = _seed(client)
    client.post(f"/v1/tasks/{task['id']}/handoff", headers={"Idempotency-Key": "h1"}, json={
        "child_task_id": child_id, "target_provider": "anthropic", "target_model": "claude-x",
        "fields": ["task_id"], "data_classification": "internal",
    })
    matches = client.get("/v1/egress?target_provider=anthropic").json()["data"]["records"]
    none = client.get("/v1/egress?target_provider=google").json()["data"]["records"]
    assert len(matches) == 1
    assert len(none) == 0
