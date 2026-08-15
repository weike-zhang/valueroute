import httpx
import pytest
import asyncio

from valueroute.frameworks.agentscope import AgentScopeFrameworkAdapter
from valueroute.frameworks.agentscope.adapter import HttpxValueRouteApi
from valueroute.providers.openai import OpenAIProviderAdapter, ProviderCallError


def test_agentscope_adapter_maps_public_contract():
    adapter = AgentScopeFrameworkAdapter()
    session = adapter.session_payload({"tenant_id": "t", "session_id": "s"})
    assert session == {"tenant_id": "t", "host_session_id": "s", "orchestration_mode": "worker_only"}
    assert adapter.event_to_host({"sequence": 2, "type": "task.started", "data": {}})["sequence"] == 2


def test_agentscope_adapter_drives_public_lifecycle():
    class FakeApi:
        def __init__(self):
            self.calls = []

        async def post(self, path, *, json, idempotency_key):
            self.calls.append((path, json, idempotency_key))
            if path == "/v1/controller-sessions":
                return {"data": {"id": "session", "version": 1}}
            if path.endswith("/epochs"):
                return {"data": {"id": "epoch"}}
            if path == "/v1/tasks":
                return {"data": {"id": "task", "version": 1}}
            if path.endswith("/execute"):
                return {"data": {"id": "task", "status": "running"}}
            raise AssertionError(path)

        async def events(self, path, *, last_event_id=None):
            return [{"id": "evt_1", "sequence": 1, "type": "task.running", "data": {}}]

    async def run():
        api = FakeApi()
        result = await AgentScopeFrameworkAdapter().start_task(api, {"tenant_id": "t", "session_id": "host", "goal": "do", "acceptance_contract": [{"id": "a", "description": "pass"}], "workspace": {"canonical_uri": "workspace://x", "base_revision": "r1"}})
        events = await AgentScopeFrameworkAdapter().resume_events(api, "task", last_event_id="evt_0")
        return result, events, api.calls

    result, events, calls = asyncio.run(run())
    assert result["task"]["status"] == "running"
    assert events[0]["event_id"] == "evt_1"
    assert [call[0] for call in calls] == ["/v1/controller-sessions", "/v1/controller-sessions/session/epochs", "/v1/tasks", "/v1/tasks/task/execute"]


def test_openai_adapter_normalizes_response_and_unknown_cost():
    async def handler(request: httpx.Request):
        assert request.url.path == "/v1/responses"
        return httpx.Response(200, json={"output_text": "done", "usage": {"input_tokens": 3, "output_tokens": 2}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async def run():
        result = await OpenAIProviderAdapter(model_id="m", api_key="secret", client=client).complete(task_id="t", input_text="hi")
        await client.aclose()
        return result
    result = asyncio.run(run())
    assert result.text == "done"
    assert result.usage.input_tokens == 3
    assert result.usage.cost_usd is None


def test_openai_adapter_retries_retryable_failures_and_records_count():
    calls = 0

    async def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"error": "temporary"})
        return httpx.Response(200, json={"output_text": "done", "usage": {"input_tokens": 1, "output_tokens": 1}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def run():
        result = await OpenAIProviderAdapter(model_id="m", api_key="secret", client=client).complete(task_id="t", input_text="hi", retries=1)
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert calls == 2
    assert result.usage.retries == 1


def test_openai_adapter_does_not_call_without_credential():
    with pytest.raises(ProviderCallError, match="credential"):
        asyncio.run(OpenAIProviderAdapter(model_id="m", api_key=None).complete(task_id="t", input_text="hi"))


def test_openai_adapter_cancel_aborts_in_flight_request():
    started = asyncio.Event()
    released = asyncio.Event()

    async def handler(request: httpx.Request):
        started.set()
        await released.wait()
        return httpx.Response(200, json={"output_text": "done", "usage": {"input_tokens": 1, "output_tokens": 1}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenAIProviderAdapter(model_id="m", api_key="secret", client=client, cancel_timeout_seconds=2.0)

    async def run():
        task = asyncio.create_task(adapter.complete(task_id="t", input_text="hi"))
        await started.wait()
        stopped = await adapter.cancel(task_id="t")
        await asyncio.gather(task, return_exceptions=True)
        await client.aclose()
        return stopped

    assert asyncio.run(run()) is True


def test_openai_adapter_cancel_returns_false_when_no_request_is_in_flight():
    adapter = OpenAIProviderAdapter(model_id="m", api_key="secret")
    assert asyncio.run(adapter.cancel(task_id="missing")) is False


def test_runner_records_cancelled_with_real_openai_adapter(tmp_path):
    from valueroute.domain.models import WorkerAttempt, WorkerAttemptStatus
    from valueroute.execution.manager import ExecutionManager
    from valueroute.execution.runner import WorkerRunner
    from valueroute.storage.journal import LocalJournal
    from valueroute.storage.store import Store

    started = asyncio.Event()

    async def handler(request: httpx.Request):
        started.set()
        await asyncio.Event().wait()

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenAIProviderAdapter(model_id="m", api_key="secret", client=client, cancel_timeout_seconds=2.0)

    journal = LocalJournal(tmp_path)
    store = Store(journal)
    attempt = WorkerAttempt(id="attempt_cancel", worker_session_id="session_1", child_task_id="child_1")
    store.attempts[attempt.id] = attempt
    store.attempt_session[attempt.id] = "controller_1"

    async def exercise():
        running = asyncio.create_task(
            WorkerRunner(store, adapter, provider_timeout=None, cancel_grace_seconds=0.5).run(
                attempt.id, task_id="task_1", input_text="do work"
            )
        )
        await started.wait()
        ExecutionManager(store).request_control(attempt.id, "cancel")
        result = await running
        await client.aclose()
        return result

    result = asyncio.run(exercise())
    assert result.status is WorkerAttemptStatus.cancelled
    journal.close()


def test_httpx_bridge_preserves_idempotency_and_sse_resume():
    class Response:
        text = 'id: evt_2\nevent: task.running\ndata: {"status":"running"}\n\n'

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"id": "task"}}

    class Client:
        def __init__(self):
            self.calls = []

        async def post(self, url, *, json, headers):
            self.calls.append(("post", url, headers))
            return Response()

        async def get(self, url, *, headers):
            self.calls.append(("get", url, headers))
            return Response()

    async def run():
        client = Client()
        api = HttpxValueRouteApi(client, base_url="http://valueroute")
        result = await api.post("/v1/tasks", json={}, idempotency_key="k")
        events = await api.events("/v1/tasks/t/events", last_event_id="evt_1")
        return result, events, client.calls

    result, events, calls = asyncio.run(run())
    assert result["data"]["id"] == "task"
    assert events[0]["id"] == "evt_2" and events[0]["data"]["status"] == "running"
    assert calls[0][2] == {"Idempotency-Key": "k"}
    assert calls[1][2] == {"Last-Event-ID": "evt_1"}
