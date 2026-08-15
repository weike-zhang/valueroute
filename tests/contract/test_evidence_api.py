from pathlib import Path

from fastapi.testclient import TestClient

from valueroute.api.app import create_app


def test_evidence_is_versioned_persisted_and_unobserved_is_not_complete(tmp_path: Path):
    client = TestClient(create_app(tmp_path))
    session = client.post("/v1/controller-sessions", headers={"Idempotency-Key": "s"}, json={"tenant_id": "t", "host_session_id": "h", "orchestration_mode": "off"}).json()["data"]
    task = client.post("/v1/tasks", headers={"Idempotency-Key": "t"}, json={"controller_session_id": session["id"], "request_type": "new_task", "goal": "verify", "acceptance_contract": [{"id": "a", "description": "live"}], "data_classification": "internal", "workspace": {"canonical_uri": "workspace://x", "base_revision": "r1"}}).json()["data"]
    response = client.post(f"/v1/tasks/{task['id']}/evidence", headers={"Idempotency-Key": "e"}, json={"expected_version": 1, "requirement_id": "a", "evidence_type": "live_check", "observation_status": "unobserved", "source": "browser"})
    assert response.status_code == 201
    assert response.json()["data"]["gate"]["can_complete"] is False
    repeated = client.post(f"/v1/tasks/{task['id']}/evidence", headers={"Idempotency-Key": "e"}, json={"expected_version": 1, "requirement_id": "a", "evidence_type": "live_check", "observation_status": "unobserved", "source": "browser"})
    assert repeated.status_code == 201
    assert len(client.get(f"/v1/tasks/{task['id']}/evidence").json()["data"]["records"]) == 1
    listed = client.get(f"/v1/tasks/{task['id']}/evidence").json()["data"]
    assert listed["records"][0]["observation_status"] == "unobserved"


def test_evidence_survives_application_restart(tmp_path: Path):
    first_app = create_app(tmp_path)
    first = TestClient(first_app)
    session = first.post("/v1/controller-sessions", headers={"Idempotency-Key": "s"}, json={"tenant_id": "t", "host_session_id": "h", "orchestration_mode": "off"}).json()["data"]
    task = first.post("/v1/tasks", headers={"Idempotency-Key": "t"}, json={"controller_session_id": session["id"], "request_type": "new_task", "goal": "verify", "acceptance_contract": [{"id": "a", "description": "live"}], "data_classification": "internal", "workspace": {"canonical_uri": "workspace://x", "base_revision": "r1"}}).json()["data"]
    first.post(f"/v1/tasks/{task['id']}/evidence", headers={"Idempotency-Key": "e"}, json={"expected_version": 1, "requirement_id": "a", "evidence_type": "test", "observation_status": "observed_pass", "source": "pytest"})
    first_app.state.store.journal.close()

    restarted = TestClient(create_app(tmp_path))
    restored = restarted.get(f"/v1/tasks/{task['id']}/evidence")
    assert restored.status_code == 200
    assert restored.json()["data"]["records"][0]["observation_status"] == "observed_pass"
    restarted.app.state.store.journal.close()


def test_parent_verification_completes_off_task_with_observed_evidence(tmp_path: Path):
    client = TestClient(create_app(tmp_path))
    session = client.post("/v1/controller-sessions", headers={"Idempotency-Key": "s"}, json={"tenant_id": "t", "host_session_id": "h", "orchestration_mode": "off"}).json()["data"]
    client.post(f"/v1/controller-sessions/{session['id']}/epochs", headers={"Idempotency-Key": "e"}, json={"expected_version": 1, "provider_id": "openai", "model_id": "test", "reasoning_effort": "low"})
    task = client.post("/v1/tasks", headers={"Idempotency-Key": "t"}, json={"controller_session_id": session["id"], "request_type": "new_task", "goal": "verify", "acceptance_contract": [{"id": "a", "description": "pass"}], "data_classification": "internal", "workspace": {"canonical_uri": "workspace://x", "base_revision": "r1"}}).json()["data"]
    running = client.post(f"/v1/tasks/{task['id']}/execute", headers={"Idempotency-Key": "x"}, json={"expected_version": 1}).json()["data"]
    evidence = client.post(f"/v1/tasks/{task['id']}/evidence", headers={"Idempotency-Key": "e"}, json={"expected_version": running["version"], "requirement_id": "a", "evidence_type": "test", "observation_status": "observed_pass", "source": "pytest"}).json()
    verified = client.post(f"/v1/tasks/{task['id']}/verify", headers={"Idempotency-Key": "v"}, json={"expected_version": evidence["meta"]["resource_version"], "changesets": []})
    assert verified.status_code == 202
    assert verified.json()["data"]["status"] == "completed"


def test_parent_verification_does_not_complete_with_unobserved_evidence(tmp_path: Path):
    client = TestClient(create_app(tmp_path))
    session = client.post("/v1/controller-sessions", headers={"Idempotency-Key": "s"}, json={"tenant_id": "t", "host_session_id": "h", "orchestration_mode": "off"}).json()["data"]
    client.post(f"/v1/controller-sessions/{session['id']}/epochs", headers={"Idempotency-Key": "e"}, json={"expected_version": 1, "provider_id": "openai", "model_id": "test", "reasoning_effort": "low"})
    task = client.post("/v1/tasks", headers={"Idempotency-Key": "t"}, json={"controller_session_id": session["id"], "request_type": "new_task", "goal": "verify", "acceptance_contract": [{"id": "a", "description": "pass"}], "data_classification": "internal", "workspace": {"canonical_uri": "workspace://x", "base_revision": "r1"}}).json()["data"]
    running = client.post(f"/v1/tasks/{task['id']}/execute", headers={"Idempotency-Key": "x"}, json={"expected_version": 1}).json()["data"]
    evidence = client.post(f"/v1/tasks/{task['id']}/evidence", headers={"Idempotency-Key": "e"}, json={"expected_version": running["version"], "requirement_id": "a", "evidence_type": "live_check", "observation_status": "unobserved", "source": "browser"}).json()
    verified = client.post(f"/v1/tasks/{task['id']}/verify", headers={"Idempotency-Key": "v"}, json={"expected_version": evidence["meta"]["resource_version"], "changesets": []})
    assert verified.status_code == 202
    assert verified.json()["data"]["status"] == "blocked"
    assert verified.json()["data"]["can_complete"] is False
