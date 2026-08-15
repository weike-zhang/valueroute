from pathlib import Path

from fastapi.testclient import TestClient

from valueroute.api.app import create_app


def advisory_payload(**overrides):
    payload = {
        "tenant_id": "t",
        "host_session_id": "h",
        "user_text": "fix the failing test in valueroute/domain",
    }
    payload.update(overrides)
    return payload


def test_advisory_returns_advice_without_shadow(tmp_path: Path):
    client = TestClient(create_app(tmp_path))
    response = client.post("/v1/advisory", json=advisory_payload())
    assert response.status_code == 202
    body = response.json()["data"]
    assert body["advice"]["envelope_id"]
    assert body["advice"]["boundary_decision"]["request_type"] == "new_task"
    assert body["advice"]["boundary_decision"]["method"] == "rule_based"
    assert any(candidate["mode"] == "direct" for candidate in body["advice"]["candidates"])
    assert body["shadow_id"] is None


def test_advisory_control_request_is_rejected(tmp_path: Path):
    client = TestClient(create_app(tmp_path))
    response = client.post("/v1/advisory", json=advisory_payload(user_text="取消任务", host_declared_request_type="control"))
    assert response.status_code == 202
    body = response.json()["data"]
    assert body["advice"]["rejected"] is True
    assert any("control" in reason for reason in body["advice"]["rejection_reasons"])


def test_advisory_requires_strict_fields(tmp_path: Path):
    client = TestClient(create_app(tmp_path))
    response = client.post("/v1/advisory", json={"tenant_id": "t", "host_session_id": "h", "user_text": "x", "unknown_field": 1})
    assert response.status_code == 422


def test_advisory_shadow_is_idempotent_with_key(tmp_path: Path):
    client = TestClient(create_app(tmp_path))
    headers = {"Idempotency-Key": "adv-1"}
    payload = advisory_payload(record_shadow=True)
    first = client.post("/v1/advisory", headers=headers, json=payload)
    second = client.post("/v1/advisory", headers=headers, json=payload)
    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["data"]["shadow_id"] == first.json()["data"]["shadow_id"]
    assert second.json()["data"]["advice"]["id"] == first.json()["data"]["advice"]["id"]


def test_shadow_records_are_listed(tmp_path: Path):
    client = TestClient(create_app(tmp_path))
    client.post("/v1/advisory", headers={"Idempotency-Key": "adv-1"}, json=advisory_payload(record_shadow=True))
    listed = client.get("/v1/advisory/shadow")
    assert listed.status_code == 200
    records = listed.json()["data"]["records"]
    assert len(records) == 1
    assert records[0]["status"] == "proposed"
    assert records[0]["envelope_hash"]


def test_shadow_record_can_be_fetched_by_id(tmp_path: Path):
    client = TestClient(create_app(tmp_path))
    created = client.post("/v1/advisory", headers={"Idempotency-Key": "adv-1"}, json=advisory_payload(record_shadow=True)).json()["data"]
    record_id = created["shadow_id"]
    fetched = client.get(f"/v1/advisory/shadow/{record_id}")
    assert fetched.status_code == 200
    assert fetched.json()["data"]["records"][0]["id"] == record_id
    missing = client.get("/v1/advisory/shadow/nope")
    assert missing.status_code == 404


def test_shadow_records_survive_app_restart(tmp_path: Path):
    client = TestClient(create_app(tmp_path))
    client.post("/v1/advisory", headers={"Idempotency-Key": "adv-1"}, json=advisory_payload(record_shadow=True))
    client.app.state.store.journal.close()
    reopened = TestClient(create_app(tmp_path))
    listed = reopened.get("/v1/advisory/shadow")
    assert listed.status_code == 200
    assert len(listed.json()["data"]["records"]) == 1
    reopened.app.state.store.journal.close()
