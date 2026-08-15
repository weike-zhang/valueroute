from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from valueroute.api.app import create_app


def test_approval_decision_is_idempotent_and_survives_restart(tmp_path: Path):
    app = create_app(tmp_path); client = TestClient(app)
    session = client.post("/v1/controller-sessions", headers={"Idempotency-Key": "s"}, json={"tenant_id": "t", "host_session_id": "h", "orchestration_mode": "off"}).json()["data"]
    task = client.post("/v1/tasks", headers={"Idempotency-Key": "t"}, json={"controller_session_id": session["id"], "request_type": "new_task", "goal": "approve", "acceptance_contract": [{"id": "a", "description": "pass"}], "data_classification": "internal", "workspace": {"canonical_uri": "workspace://x", "base_revision": "r1"}}).json()["data"]
    expires = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    approval = client.post(f"/v1/tasks/{task['id']}/approvals", headers={"Idempotency-Key": "a"}, json={"action_summary": "run tool", "risk": "high", "expires_at": expires}).json()["data"]
    assert approval["version"] == 1
    decided = client.post(f"/v1/tasks/{task['id']}/approvals/{approval['id']}", headers={"Idempotency-Key": "d"}, json={"expected_version": 1, "decision": "approve"})
    assert decided.status_code == 202 and decided.json()["data"]["status"] == "approved"
    assert decided.json()["data"]["version"] == 2
    repeated = client.post(f"/v1/tasks/{task['id']}/approvals/{approval['id']}", headers={"Idempotency-Key": "d"}, json={"expected_version": 1, "decision": "approve"})
    assert repeated.status_code == 202 and repeated.json()["data"] == decided.json()["data"]
    conflict = client.post(f"/v1/tasks/{task['id']}/approvals/{approval['id']}", headers={"Idempotency-Key": "d2"}, json={"expected_version": 1, "decision": "reject"})
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "version_conflict"
    missing = client.post(f"/v1/tasks/{task['id']}/approvals/{approval['id']}", headers={"Idempotency-Key": "d3"}, json={"decision": "reject"})
    assert missing.status_code == 422
    app.state.store.journal.close()
    restarted = TestClient(create_app(tmp_path))
    replayed = restarted.app.state.store.approvals[approval["id"]]
    assert replayed.status.value == "approved" and replayed.version == 2
    restarted.app.state.store.journal.close()
