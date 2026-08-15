from pathlib import Path

from fastapi.testclient import TestClient

from valueroute.api.app import create_app


def test_owner_review_and_verifier_api_persist_scoped_evidence(tmp_path: Path):
    app = create_app(tmp_path)
    client = TestClient(app)
    session = client.post(
        "/v1/controller-sessions",
        headers={"Idempotency-Key": "s"},
        json={"tenant_id": "t", "host_session_id": "h", "orchestration_mode": "off"},
    ).json()["data"]
    client.post(
        f"/v1/controller-sessions/{session['id']}/epochs",
        headers={"Idempotency-Key": "e"},
        json={"expected_version": 1, "provider_id": "p", "model_id": "m", "reasoning_effort": "low"},
    )
    task = client.post(
        "/v1/tasks",
        headers={"Idempotency-Key": "t"},
        json={
            "controller_session_id": session["id"],
            "request_type": "new_task",
            "goal": "review",
            "acceptance_contract": [{"id": "a", "description": "pass"}],
            "data_classification": "internal",
            "workspace": {"canonical_uri": "workspace://x", "base_revision": "r1"},
        },
    ).json()["data"]
    region = {"resource_kind": "file", "resource_id": "src/app.py", "selector_type": "whole_resource", "selector_value": "", "base_revision": "r1"}
    client.post(
        f"/v1/tasks/{task['id']}/plan",
        headers={"Idempotency-Key": "plan"},
        json={"expected_parent_version": 1, "children": [{"client_ref": "app", "objective": "repair", "write_regions": [region], "acceptance_contract": ["pass"]}], "integration_order": ["app"]},
    )
    child_id = app.state.store.tasks[task["id"]].child_task_ids[0]
    app.state.ownership.assign(child_id, "owner-1", [app.state.store.children[child_id].write_regions[0]])
    current = app.state.store.tasks[task["id"]]
    evidence = client.post(
        f"/v1/tasks/{task['id']}/evidence",
        headers={"Idempotency-Key": "ev"},
        json={"expected_version": current.version, "requirement_id": "a", "evidence_type": "test", "observation_status": "observed_pass", "source": "pytest", "child_task_id": child_id, "region": region},
    ).json()
    review = client.post(
        f"/v1/tasks/{task['id']}/children/{child_id}/review",
        headers={"Idempotency-Key": "review"},
        json={"expected_assignment_version": 1, "owner_agent_id": "owner-1", "review_regions": [region], "evidence_ids": [evidence["data"]["evidence"]["id"]], "summary": "checked"},
    )
    assert review.status_code == 201, review.text
    review_data = review.json()["data"]
    verified = client.post(
        f"/v1/tasks/{task['id']}/children/{child_id}/reviews/{review_data['id']}/verify",
        headers={"Idempotency-Key": "verify"},
        json={"expected_review_version": 1, "verifier_agent_id": "owner-1", "evidence_ids": [evidence["data"]["evidence"]["id"]]},
    )
    assert verified.status_code == 201, verified.text
    assert verified.json()["data"]["status"] == "passed"
    app.state.store.journal.close()
