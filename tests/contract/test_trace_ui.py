from pathlib import Path

from fastapi.testclient import TestClient

from valueroute.api.app import create_app


def test_trace_ui_renders_empty_store(tmp_path: Path):
    client = TestClient(create_app(tmp_path))
    response = client.get("/v1/trace/ui")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "ValueRoute runtime trace" in body
    assert "none" in body


def test_trace_ui_escapes_rendered_model_id(tmp_path: Path):
    from valueroute.routing.manifest import ModelProfile

    profiles = [ModelProfile.model_validate({
        "provider_id": "openai", "model_id": "<img src=x onerror=alert(1)>",
        "measured_at": "2026-08-15T00:00:00Z", "protocol_status": "compatible",
        "worker_status": "candidate", "controller_status": "certified",
        "supported_modalities": ["text"], "supported_tools": [],
        "effort_mapping": {}, "region": "test", "evidence_refs": [],
    })]
    client = TestClient(create_app(tmp_path, controller_profiles=profiles))
    session = client.post(
        "/v1/controller-sessions",
        headers={"Idempotency-Key": "s"},
        json={"tenant_id": "t", "host_session_id": "h", "orchestration_mode": "automatic"},
    ).json()["data"]
    epoch = client.post(f"/v1/controller-sessions/{session['id']}/epochs/automatic", headers={"Idempotency-Key": "a"}, json={"expected_version": 1}).json()["data"]

    response = client.get("/v1/trace/ui")
    assert response.status_code == 200
    assert "<img src=x onerror=alert(1)>" not in response.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in response.text
    assert epoch["id"] in response.text


def test_trace_ui_shows_automatic_epoch(tmp_path: Path):
    from valueroute.routing.manifest import ModelProfile

    profiles = [ModelProfile.model_validate({
        "provider_id": "openai", "model_id": "m1",
        "measured_at": "2026-08-15T00:00:00Z", "protocol_status": "compatible",
        "worker_status": "candidate", "controller_status": "certified",
        "supported_modalities": ["text"], "supported_tools": [],
        "effort_mapping": {}, "region": "test", "evidence_refs": [],
    })]
    client = TestClient(create_app(tmp_path, controller_profiles=profiles))
    session = client.post(
        "/v1/controller-sessions",
        headers={"Idempotency-Key": "s"},
        json={"tenant_id": "t", "host_session_id": "h", "orchestration_mode": "automatic"},
    ).json()["data"]
    epoch = client.post(f"/v1/controller-sessions/{session['id']}/epochs/automatic", headers={"Idempotency-Key": "a"}, json={"expected_version": 1}).json()["data"]

    response = client.get("/v1/trace/ui")
    assert response.status_code == 200
    assert epoch["id"] in response.text
    assert "openai/m1" in response.text
