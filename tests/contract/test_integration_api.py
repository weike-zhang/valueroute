from pathlib import Path

from fastapi.testclient import TestClient

from valueroute.api.app import create_app
from valueroute.domain.models import IntegrationAttempt, IntegrationAttemptStatus


def test_task_integration_attempts_and_result_are_queryable_and_drive_parent_verification(tmp_path: Path):
    app = create_app(tmp_path)
    client = TestClient(app)
    session = client.post(
        "/v1/controller-sessions",
        headers={"Idempotency-Key": "s"},
        json={"tenant_id": "t", "host_session_id": "h", "orchestration_mode": "off"},
    ).json()["data"]
    client.post(
        f"/v1/controller-sessions/{session['id']}/epochs",
        headers={"Idempotency-Key": "epoch"},
        json={"expected_version": 1, "provider_id": "openai", "model_id": "test", "reasoning_effort": "low"},
    )
    task = client.post(
        "/v1/tasks",
        headers={"Idempotency-Key": "t"},
        json={
            "controller_session_id": session["id"],
            "request_type": "new_task",
            "goal": "integrate",
            "acceptance_contract": [{"id": "a", "description": "pass"}],
            "data_classification": "internal",
            "workspace": {"canonical_uri": "workspace://x", "base_revision": "r1"},
        },
    ).json()["data"]
    plan = client.post(
        f"/v1/tasks/{task['id']}/plan",
        headers={"Idempotency-Key": "p"},
        json={
            "expected_parent_version": 1,
            "children": [{"client_ref": "backend", "objective": "backend", "acceptance_contract": ["pass"]}],
            "integration_order": ["backend"],
        },
    ).json()
    running = client.post(
        f"/v1/tasks/{task['id']}/execute",
        headers={"Idempotency-Key": "x"},
        json={"expected_version": plan["meta"]["resource_version"]},
    ).json()["data"]
    evidence = client.post(
        f"/v1/tasks/{task['id']}/evidence",
        headers={"Idempotency-Key": "e"},
        json={
            "expected_version": running["version"],
            "requirement_id": "a",
            "evidence_type": "test",
            "observation_status": "observed_pass",
            "source": "pytest",
        },
    ).json()

    app.state.store.record_integration_attempt(
        IntegrationAttempt(
            id="ia-backend",
            parent_task_id=task["id"],
            client_ref="backend",
            order_index=0,
            status=IntegrationAttemptStatus.integrated,
            revision="r2",
        ),
        "integration.completed",
    )

    attempts = client.get(f"/v1/tasks/{task['id']}/integration-attempts")
    assert attempts.status_code == 200
    assert attempts.json()["data"]["attempts"][0]["status"] == "integrated"
    result = client.get(f"/v1/tasks/{task['id']}/integration-result")
    assert result.status_code == 200
    assert result.json()["data"]["result"] == [{"client_ref": "backend", "status": "integrated", "revision": "r2"}]

    verified = client.post(
        f"/v1/tasks/{task['id']}/verify",
        headers={"Idempotency-Key": "v"},
        json={
            "expected_version": evidence["meta"]["resource_version"],
            "changesets": [{"conflict": True}],
        },
    )
    assert verified.status_code == 202
    assert verified.json()["data"]["status"] == "completed"
    app.state.store.journal.close()

    restarted = TestClient(create_app(tmp_path))
    restored = restarted.get(f"/v1/tasks/{task['id']}/integration-result")
    assert restored.status_code == 200
    assert restored.json()["data"]["result"][0]["revision"] == "r2"
    restarted.app.state.store.journal.close()
