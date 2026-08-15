from pathlib import Path

from fastapi.testclient import TestClient

from valueroute.api.app import create_app
from valueroute.routing.manifest import ModelProfile


def certified(*, model_id: str) -> dict:
    return ModelProfile.model_validate({
        "provider_id": "openai",
        "model_id": model_id,
        "measured_at": "2026-08-15T00:00:00Z",
        "protocol_status": "compatible",
        "worker_status": "candidate",
        "controller_status": "certified",
        "supported_modalities": ["text"],
        "supported_tools": [],
        "effort_mapping": {},
        "region": "test",
        "evidence_refs": [],
    })


def make_client(tmp_path: Path, models: list[str] | None = None) -> TestClient:
    profiles = [certified(model_id=m) for m in (models or ["m1", "m2"])]
    return TestClient(create_app(tmp_path, controller_profiles=profiles))


def test_automatic_ensure_selects_certified_controller_and_is_sticky(tmp_path: Path):
    client = make_client(tmp_path)
    session = client.post(
        "/v1/controller-sessions",
        headers={"Idempotency-Key": "s"},
        json={"tenant_id": "t", "host_session_id": "h", "orchestration_mode": "automatic"},
    ).json()["data"]

    first = client.post(f"/v1/controller-sessions/{session['id']}/epochs/automatic", headers={"Idempotency-Key": "a"}, json={"expected_version": 1}).json()["data"]
    assert first["status"] == "active"
    assert first["model_id"] in {"m1", "m2"}

    second = client.post(f"/v1/controller-sessions/{session['id']}/epochs/automatic", headers={"Idempotency-Key": "b"}, json={"expected_version": 2}).json()["data"]
    assert second["id"] == first["id"]


def test_automatic_ensure_fails_closed_without_certified_controller(tmp_path: Path):
    client = TestClient(create_app(tmp_path, controller_profiles=[]))
    session = client.post(
        "/v1/controller-sessions",
        headers={"Idempotency-Key": "s"},
        json={"tenant_id": "t", "host_session_id": "h", "orchestration_mode": "automatic"},
    ).json()["data"]
    response = client.post(f"/v1/controller-sessions/{session['id']}/epochs/automatic", headers={"Idempotency-Key": "a"}, json={"expected_version": 1})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "no_certified_controller"


def test_automatic_ensure_requires_automatic_mode(tmp_path: Path):
    client = make_client(tmp_path)
    session = client.post(
        "/v1/controller-sessions",
        headers={"Idempotency-Key": "s"},
        json={"tenant_id": "t", "host_session_id": "h", "orchestration_mode": "off"},
    ).json()["data"]
    response = client.post(f"/v1/controller-sessions/{session['id']}/epochs/automatic", headers={"Idempotency-Key": "a"}, json={"expected_version": 1})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "not_automatic_mode"


def test_automatic_switch_releases_and_activates_with_idempotency(tmp_path: Path):
    client = make_client(tmp_path)
    session = client.post(
        "/v1/controller-sessions",
        headers={"Idempotency-Key": "s"},
        json={"tenant_id": "t", "host_session_id": "h", "orchestration_mode": "automatic"},
    ).json()["data"]
    first = client.post(f"/v1/controller-sessions/{session['id']}/epochs/automatic", headers={"Idempotency-Key": "a"}, json={"expected_version": 1}).json()["data"]

    switched = client.post(f"/v1/controller-sessions/{session['id']}/epochs/switch", headers={"Idempotency-Key": "w"}, json={"expected_version": 2}).json()["data"]
    assert switched["id"] != first["id"]
    assert switched["status"] == "active"

    # same switch with the same Idempotency-Key and payload returns the same epoch (replay)
    replayed = client.post(f"/v1/controller-sessions/{session['id']}/epochs/switch", headers={"Idempotency-Key": "w"}, json={"expected_version": 2}).json()["data"]
    assert replayed["id"] == switched["id"]


def test_automatic_switch_blocked_while_task_running(tmp_path: Path):
    client = make_client(tmp_path)
    session = client.post(
        "/v1/controller-sessions",
        headers={"Idempotency-Key": "s"},
        json={"tenant_id": "t", "host_session_id": "h", "orchestration_mode": "automatic"},
    ).json()["data"]
    client.post(f"/v1/controller-sessions/{session['id']}/epochs/automatic", headers={"Idempotency-Key": "a"}, json={"expected_version": 1})

    task = client.post("/v1/tasks", headers={"Idempotency-Key": "t"}, json={
        "controller_session_id": session["id"],
        "request_type": "new_task",
        "goal": "busy",
        "acceptance_contract": [{"id": "a", "description": "pass"}],
        "data_classification": "internal",
        "workspace": {"canonical_uri": "workspace://x", "base_revision": "r1"},
    }).json()["data"]
    client.post(f"/v1/tasks/{task['id']}/execute", headers={"Idempotency-Key": "x"}, json={"expected_version": 1})

    response = client.post(f"/v1/controller-sessions/{session['id']}/epochs/switch", headers={"Idempotency-Key": "w"}, json={"expected_version": 2})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "session_busy"


def test_automatic_epoch_survives_restart(tmp_path: Path):
    client = make_client(tmp_path)
    session = client.post(
        "/v1/controller-sessions",
        headers={"Idempotency-Key": "s"},
        json={"tenant_id": "t", "host_session_id": "h", "orchestration_mode": "automatic"},
    ).json()["data"]
    first = client.post(f"/v1/controller-sessions/{session['id']}/epochs/automatic", headers={"Idempotency-Key": "a"}, json={"expected_version": 1}).json()["data"]
    session_id = session["id"]
    client.app.state.store.journal.close()

    restarted = TestClient(create_app(tmp_path, controller_profiles=[certified(model_id="m1"), certified(model_id="m2")]))
    ensure = restarted.post(f"/v1/controller-sessions/{session_id}/epochs/automatic", headers={"Idempotency-Key": "b"}, json={"expected_version": 2}).json()["data"]
    assert ensure["id"] == first["id"]
    restarted.app.state.store.journal.close()
