from pathlib import Path

from fastapi.testclient import TestClient

from valueroute.api.app import create_app
from valueroute.workspaces.local import LocalWorkspaceAdapter
from valueroute.observability.usage import UsageRecord


class CompletingProvider:
    async def complete(self, **kwargs):
        return type("ProviderResult", (), {"usage": UsageRecord(id="u_http_supervisor", task_id=kwargs["task_id"], provider_id="test", model_id="m", latency_ms=1)})()


def test_worker_only_execute_persists_queued_attempts(tmp_path: Path):
    client = TestClient(create_app(tmp_path))
    session = client.post("/v1/controller-sessions", headers={"Idempotency-Key": "s"}, json={"tenant_id": "t", "host_session_id": "h", "orchestration_mode": "worker_only"}).json()["data"]
    client.post(f"/v1/controller-sessions/{session['id']}/epochs", headers={"Idempotency-Key": "e"}, json={"expected_version": 1, "provider_id": "openai", "model_id": "test", "reasoning_effort": "low"})
    task = client.post("/v1/tasks", headers={"Idempotency-Key": "t"}, json={"controller_session_id": session["id"], "request_type": "new_task", "goal": "run", "acceptance_contract": [{"id": "a", "description": "pass"}], "data_classification": "internal", "workspace": {"canonical_uri": "workspace://x", "base_revision": "r1"}}).json()["data"]
    plan = {"expected_parent_version": 1, "children": [{"client_ref": "backend", "objective": "backend", "acceptance_contract": ["pass"]}], "integration_order": ["backend"]}
    planned = client.post(f"/v1/tasks/{task['id']}/plan", headers={"Idempotency-Key": "p"}, json=plan).json()["data"]
    executed = client.post(f"/v1/tasks/{task['id']}/execute", headers={"Idempotency-Key": "x"}, json={"expected_version": planned["plan"]["version"] + 1}).json()
    assert executed["data"]["status"] == "running"
    assert len(client.app.state.store.attempts) == 1
    assert next(iter(client.app.state.store.attempts.values())).status.value == "queued"
    repeated = client.post(f"/v1/tasks/{task['id']}/execute", headers={"Idempotency-Key": "x"}, json={"expected_version": planned["plan"]["version"] + 1})
    assert repeated.status_code == 202
    assert len(client.app.state.store.attempts) == 1


def test_worker_attempt_is_bound_to_isolated_local_workspace(tmp_path: Path):
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    (canonical / "README.md").write_text("base")
    revision = LocalWorkspaceAdapter(canonical, tmp_path / "workers-probe").snapshot().revision
    client = TestClient(create_app(tmp_path / "service"))
    session = client.post("/v1/controller-sessions", headers={"Idempotency-Key": "s"}, json={"tenant_id": "t", "host_session_id": "h", "orchestration_mode": "worker_only"}).json()["data"]
    client.post(f"/v1/controller-sessions/{session['id']}/epochs", headers={"Idempotency-Key": "e"}, json={"expected_version": 1, "provider_id": "openai", "model_id": "test", "reasoning_effort": "low"})
    task = client.post("/v1/tasks", headers={"Idempotency-Key": "t"}, json={"controller_session_id": session["id"], "request_type": "new_task", "goal": "isolated", "acceptance_contract": [{"id": "a", "description": "pass"}], "data_classification": "internal", "workspace": {"canonical_uri": str(canonical), "base_revision": revision}}).json()["data"]
    plan = {"expected_parent_version": 1, "children": [{"client_ref": "worker", "objective": "edit", "acceptance_contract": ["pass"]}], "integration_order": ["worker"]}
    planned = client.post(f"/v1/tasks/{task['id']}/plan", headers={"Idempotency-Key": "p"}, json=plan).json()["data"]
    client.post(f"/v1/tasks/{task['id']}/execute", headers={"Idempotency-Key": "x"}, json={"expected_version": planned["plan"]["version"] + 1})
    attempt = next(iter(client.app.state.store.attempts.values()))
    assert attempt.workspace is not None
    assert Path(attempt.workspace.owner_workspace).is_dir()
    assert Path(attempt.workspace.owner_workspace).resolve() != canonical.resolve()
    assert (canonical / "README.md").read_text() == "base"


def test_http_app_can_own_supervisor_lifecycle_when_provider_is_injected(tmp_path: Path):
    from time import monotonic, sleep

    from valueroute.api.app import create_app

    app = create_app(tmp_path / "supervised", provider=CompletingProvider())
    with TestClient(app) as client:
        assert client.get("/v1/supervisor/status").json()["enabled"] is True
        session = client.post("/v1/controller-sessions", headers={"Idempotency-Key": "s"}, json={"tenant_id": "t", "host_session_id": "h", "orchestration_mode": "worker_only"}).json()["data"]
        client.post(f"/v1/controller-sessions/{session['id']}/epochs", headers={"Idempotency-Key": "e"}, json={"expected_version": 1, "provider_id": "test", "model_id": "m", "reasoning_effort": "low"})
        task = client.post("/v1/tasks", headers={"Idempotency-Key": "t"}, json={"controller_session_id": session["id"], "request_type": "new_task", "goal": "supervise", "acceptance_contract": [{"id": "a", "description": "pass"}], "data_classification": "internal", "workspace": {"canonical_uri": "workspace://x", "base_revision": "r1"}}).json()["data"]
        planned = client.post(f"/v1/tasks/{task['id']}/plan", headers={"Idempotency-Key": "p"}, json={"expected_parent_version": 1, "children": [{"client_ref": "worker", "objective": "do work", "acceptance_contract": ["pass"]}], "integration_order": ["worker"]}).json()["data"]
        client.post(f"/v1/tasks/{task['id']}/execute", headers={"Idempotency-Key": "x"}, json={"expected_version": planned["plan"]["version"] + 1})
        deadline = monotonic() + 3
        while monotonic() < deadline and any(attempt.status.value not in {"succeeded", "failed"} for attempt in app.state.store.attempts.values()):
            sleep(0.02)
        assert next(iter(app.state.store.attempts.values())).status.value == "succeeded"
