import csv
import io
from pathlib import Path

from fastapi.testclient import TestClient

from valueroute.api.app import create_app
from valueroute.observability import CostStatus, UsageRecord


def create_task(client: TestClient) -> dict:
    session = client.post(
        "/v1/controller-sessions",
        headers={"Idempotency-Key": "session"},
        json={"tenant_id": "tenant", "host_session_id": "host", "orchestration_mode": "off"},
    ).json()["data"]
    return client.post(
        "/v1/tasks",
        headers={"Idempotency-Key": "task"},
        json={
            "controller_session_id": session["id"],
            "request_type": "new_task",
            "goal": "usage contract",
            "acceptance_contract": [{"id": "a", "description": "pass"}],
            "data_classification": "internal",
            "workspace": {"canonical_uri": "workspace://x", "base_revision": "r1"},
        },
    ).json()["data"]


def seed_usage(client: TestClient, task_id: str) -> None:
    client.app.state.store.record_usage(
        UsageRecord(
            id="usage_1",
            task_id=task_id,
            provider_id="provider-a",
            model_id="model-a",
            input_tokens=10,
            cached_input_tokens=2,
            output_tokens=4,
            reasoning_tokens=1,
            latency_ms=123,
            retries=2,
        )
    )
    client.app.state.store.record_usage(
        UsageRecord(
            id="usage_2",
            task_id=task_id,
            provider_id="provider-b",
            model_id="model-b",
            input_tokens=5,
            output_tokens=3,
            latency_ms=45,
            retries=1,
            cost_status=CostStatus.known,
            cost_usd=0.25,
        )
    )


def test_task_usage_query_exposes_trace_fields_and_unknown_cost_without_zero(tmp_path: Path):
    client = TestClient(create_app(tmp_path))
    task = create_task(client)
    seed_usage(client, task["id"])

    response = client.get(f"/v1/tasks/{task['id']}/usage")

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["records"][0]["provider_id"] == "provider-a"
    assert payload["records"][0]["model_id"] == "model-a"
    assert payload["records"][0]["latency_ms"] == 123
    assert payload["records"][0]["retries"] == 2
    assert payload["records"][0]["cost_status"] == "unknown"
    assert payload["records"][0]["cost_usd"] is None
    assert payload["totals"] == {
        "input_tokens": 15,
        "cached_input_tokens": 2,
        "output_tokens": 7,
        "reasoning_tokens": 1,
        "latency_ms": 168,
        "retries": 3,
        "cost_status": "unknown",
        "cost_usd": None,
    }


def test_task_usage_export_is_read_only_csv_with_trace_fields_and_blank_unknown_cost(tmp_path: Path):
    client = TestClient(create_app(tmp_path))
    task = create_task(client)
    seed_usage(client, task["id"])

    response = client.get(f"/v1/tasks/{task['id']}/usage/export")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert rows[0]["provider_id"] == "provider-a"
    assert rows[0]["model_id"] == "model-a"
    assert rows[0]["latency_ms"] == "123"
    assert rows[0]["retries"] == "2"
    assert rows[0]["cost_status"] == "unknown"
    assert rows[0]["cost_usd"] == ""
    assert rows[1]["cost_status"] == "known"
    assert rows[1]["cost_usd"] == "0.25"


def test_task_usage_export_does_not_include_secret_or_private_task_body(tmp_path: Path):
    client = TestClient(create_app(tmp_path))
    task = create_task(client)
    secret = "sk-export-secret-do-not-leak"
    private_body = "private-task-body-do-not-leak"

    client.app.state.store.tasks[task["id"]] = client.app.state.store.tasks[task["id"]].model_copy(
        update={"goal": private_body}
    )
    client.app.state.store.record_usage(
        UsageRecord(
            id="usage_sensitive",
            task_id=task["id"],
            provider_id="provider-a",
            model_id="model-a",
            latency_ms=1,
        )
    )

    response = client.get(f"/v1/tasks/{task['id']}/usage/export")

    assert response.status_code == 200
    assert secret not in response.text
    assert private_body not in response.text
    assert "goal" not in response.text
    assert "input_text" not in response.text


def test_task_usage_query_and_export_return_not_found_for_unknown_task(tmp_path: Path):
    client = TestClient(create_app(tmp_path))

    assert client.get("/v1/tasks/missing/usage").status_code == 404
    assert client.get("/v1/tasks/missing/usage/export").status_code == 404
