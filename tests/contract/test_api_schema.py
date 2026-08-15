from pathlib import Path

from fastapi.testclient import TestClient

from valueroute.api.app import create_app


def session_payload() -> dict[str, str]:
    return {"tenant_id": "tenant", "host_session_id": "host", "orchestration_mode": "off"}


def test_write_requests_have_stable_schema_and_idempotency_errors(tmp_path: Path):
    client = TestClient(create_app(tmp_path))

    missing_key = client.post("/v1/controller-sessions", json=session_payload())
    assert missing_key.status_code == 400
    assert missing_key.json()["detail"]["code"] == "missing_idempotency_key"

    invalid_key = client.post(
        "/v1/controller-sessions",
        headers={"Idempotency-Key": " "},
        json=session_payload(),
    )
    assert invalid_key.status_code == 400
    assert invalid_key.json()["detail"]["code"] == "invalid_idempotency_key"

    unknown_field = client.post(
        "/v1/controller-sessions",
        headers={"Idempotency-Key": "session-1"},
        json=session_payload() | {"unexpected": True},
    )
    assert unknown_field.status_code == 422
    assert unknown_field.json()["detail"]["code"] == "invalid_request"


def test_expected_version_is_strict_and_conflicts_are_stable(tmp_path: Path):
    client = TestClient(create_app(tmp_path))
    session = client.post(
        "/v1/controller-sessions",
        headers={"Idempotency-Key": "session-1"},
        json=session_payload(),
    ).json()["data"]

    missing = client.post(
        f"/v1/controller-sessions/{session['id']}/epochs",
        headers={"Idempotency-Key": "epoch-missing"},
        json={"provider_id": "openai", "model_id": "test", "reasoning_effort": "low"},
    )
    assert missing.status_code == 422
    assert missing.json()["detail"]["code"] == "invalid_boundary"

    wrong_type = client.post(
        f"/v1/controller-sessions/{session['id']}/epochs",
        headers={"Idempotency-Key": "epoch-type"},
        json={"expected_version": "1", "provider_id": "openai", "model_id": "test", "reasoning_effort": "low"},
    )
    assert wrong_type.status_code == 422
    assert wrong_type.json()["detail"]["code"] == "invalid_expected_version"

    conflict = client.post(
        f"/v1/controller-sessions/{session['id']}/epochs",
        headers={"Idempotency-Key": "epoch-conflict"},
        json={"expected_version": 2, "provider_id": "openai", "model_id": "test", "reasoning_effort": "low"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "version_conflict"
