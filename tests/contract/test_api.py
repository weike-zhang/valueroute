import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from valueroute.api.app import create_app
from valueroute.storage.journal import LocalJournal
from valueroute.storage.store import Store


class RecordingStateStore:
    """A structural StateStore adapter that records API persistence calls."""

    def __init__(self, delegate: Store):
        self.delegate = delegate
        self.commits: list[dict] = []

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def commit(self, event, **kwargs):
        self.commits.append(event)
        return self.delegate.commit(event, **kwargs)

    def commit_frame(self, events, **kwargs):
        self.commits.extend(events)
        return self.delegate.commit_frame(events, **kwargs)


class RecordingArtifactStore:
    def __init__(self):
        self.put_calls = 0

    def put(self, content: bytes, **kwargs):
        self.put_calls += 1
        return None

    def get(self, reference):
        raise AssertionError("contract fixture does not serve artifacts")

    def verify(self, reference):
        return reference


class RecordingCheckpointStore:
    def __init__(self):
        self.load_calls = 0

    def save(self, checkpoint):
        return None

    def load(self, checkpoint_id):
        self.load_calls += 1
        raise AssertionError("contract fixture has no checkpoint")

    def list_ids(self):
        return []

    def list_valid(self):
        return []


def test_create_app_uses_injected_storage_adapters(tmp_path: Path):
    journal = LocalJournal(tmp_path / "delegate")
    delegate = Store(journal)
    state = RecordingStateStore(delegate)
    artifacts = RecordingArtifactStore()
    checkpoints = RecordingCheckpointStore()
    app = create_app(
        tmp_path / "app",
        state_store=state,
        artifact_store=artifacts,
        checkpoint_store=checkpoints,
    )
    client = TestClient(app)

    response = client.post(
        "/v1/controller-sessions",
        headers={"Idempotency-Key": "injected-session"},
        json={"tenant_id": "t", "host_session_id": "h", "orchestration_mode": "off"},
    )

    assert response.status_code == 201
    assert app.state.store is state
    assert app.state.state_store is state
    assert app.state.artifact_store is artifacts
    assert app.state.checkpoint_store is checkpoints
    assert any(event["type"] == "session.created" for event in state.commits)
    app.state.store.journal.close()
    journal.close()


def test_health_and_task_idempotency(tmp_path: Path):
    client = TestClient(create_app(tmp_path))
    assert client.get("/v1/health/live").json()["status"] == "ok"
    session = client.post("/v1/controller-sessions", headers={"Idempotency-Key": "s1"}, json={"tenant_id": "t", "host_session_id": "h", "orchestration_mode": "worker_only"})
    assert session.status_code == 201
    payload = {"controller_session_id": session.json()["data"]["id"], "request_type": "new_task", "goal": "test", "acceptance_contract": [{"id": "a", "description": "pass"}], "data_classification": "internal", "workspace": {"canonical_uri": "workspace://x", "base_revision": "r1"}}
    first = client.post("/v1/tasks", headers={"Idempotency-Key": "t1"}, json=payload)
    second = client.post("/v1/tasks", headers={"Idempotency-Key": "t1"}, json=payload)
    assert first.status_code == second.status_code == 201
    assert first.json()["data"]["id"] == second.json()["data"]["id"]


def test_invalid_plan_is_rejected(tmp_path: Path):
    client = TestClient(create_app(tmp_path))
    session = client.post("/v1/controller-sessions", headers={"Idempotency-Key": "s1"}, json={"tenant_id": "t", "host_session_id": "h", "orchestration_mode": "worker_only"}).json()["data"]["id"]
    task = client.post("/v1/tasks", headers={"Idempotency-Key": "t1"}, json={"controller_session_id": session, "request_type": "new_task", "goal": "test", "acceptance_contract": [{"id": "a", "description": "pass"}], "data_classification": "internal", "workspace": {"canonical_uri": "workspace://x", "base_revision": "r1"}}).json()["data"]
    response = client.post(f"/v1/tasks/{task['id']}/plan", headers={"Idempotency-Key": "p1"}, json={"expected_parent_version": 1, "children": [{"client_ref": "a", "objective": "x", "depends_on": ["missing"], "acceptance_contract": ["pass"]}], "integration_order": ["a"]})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_plan"


def test_task_events_resume_from_last_event_id_without_replaying_earlier_events(tmp_path: Path):
    client = TestClient(create_app(tmp_path))
    session = client.post("/v1/controller-sessions", headers={"Idempotency-Key": "s"}, json={"tenant_id": "t", "host_session_id": "h", "orchestration_mode": "off"}).json()["data"]
    task = client.post("/v1/tasks", headers={"Idempotency-Key": "t"}, json={"controller_session_id": session["id"], "request_type": "new_task", "goal": "events", "acceptance_contract": [{"id": "a", "description": "pass"}], "data_classification": "internal", "workspace": {"canonical_uri": "workspace://x", "base_revision": "r1"}}).json()["data"]
    all_events = client.get(f"/v1/tasks/{task['id']}/events")
    assert all_events.status_code == 200
    assert "id: " in all_events.text and task["id"] in all_events.text
    first_id = int(all_events.text.split("id: ", 1)[1].split("\n", 1)[0])
    resumed = client.get(f"/v1/tasks/{task['id']}/events", headers={"Last-Event-ID": str(first_id)})
    assert resumed.status_code == 200
    assert task["id"] not in resumed.text
    invalid = client.get(f"/v1/tasks/{task['id']}/events", headers={"Last-Event-ID": "-1"})
    assert invalid.status_code == 400


def test_task_events_follow_delivers_a_later_journal_event(tmp_path: Path):
    app = create_app(tmp_path)
    client = TestClient(app)
    session = client.post("/v1/controller-sessions", headers={"Idempotency-Key": "s"}, json={"tenant_id": "t", "host_session_id": "h", "orchestration_mode": "off"}).json()["data"]
    task = client.post("/v1/tasks", headers={"Idempotency-Key": "t"}, json={"controller_session_id": session["id"], "request_type": "new_task", "goal": "follow", "acceptance_contract": [{"id": "a", "description": "pass"}], "data_classification": "internal", "workspace": {"canonical_uri": "workspace://x", "base_revision": "r1"}}).json()["data"]

    def append_later():
        time.sleep(0.05)
        app.state.store.commit({"type": "task.follow_probe", "data": {"task_id": task["id"], "probe": True}})

    thread = threading.Thread(target=append_later)
    thread.start()
    with client.stream("GET", f"/v1/tasks/{task['id']}/events", params={"follow": "true", "timeout_seconds": "1"}) as response:
        body = response.read().decode()
    thread.join(timeout=2)
    assert response.status_code == 200
    assert "task.follow_probe" in body


def test_leases_are_queryable_without_exposing_mutation(tmp_path: Path):
    app = create_app(tmp_path)
    client = TestClient(app)
    from valueroute.domain.models import ResourceRegion, WriterLease

    lease = WriterLease(
        id="lease_api",
        child_task_id="child_api",
        owner_agent_id="owner_api",
        region=ResourceRegion(resource_kind="file", resource_id="src/app.py", selector_type="whole_resource", selector_value="", base_revision="r1"),
    )
    app.state.store.leases[lease.id] = lease
    app.state.store.commit({"type": "lease.acquired", "data": lease.model_dump(mode="json")})
    response = client.get("/v1/leases", params={"owner_id": "owner_api"})
    assert response.status_code == 200
    assert response.json()["data"]["leases"][0]["id"] == "lease_api"
    app.state.store.journal.close()
