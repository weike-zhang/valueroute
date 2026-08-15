import asyncio

import httpx

from valueroute.api.app import create_app
import pytest

from valueroute.frameworks.agentscope import AgentScopeHost, AgentScopeLifecycleError, AgentScopeTaskHandle, HttpxValueRouteApi


def test_agentscope_adapter_drives_real_asgi_lifecycle(tmp_path):
    app = create_app(tmp_path)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://valueroute") as client:
            host = AgentScopeHost(HttpxValueRouteApi(client))
            handle = await host.create(
                {
                    "tenant_id": "tenant",
                    "session_id": "agentscope-host",
                    "goal": "exercise lifecycle",
                    "acceptance_contract": [{"id": "a", "description": "pass"}],
                    "workspace": {"canonical_uri": "workspace://test", "base_revision": "r1"},
                    "orchestration_mode": "off",
                    "provider_id": "openai",
                    "model_id": "test",
                    "reasoning_effort": "low",
                },
            )
            events = await host.subscribe(handle)
            paused = await host.pause(handle, idempotency_key="agentscope:pause")
            resumed = await host.resume(handle, idempotency_key="agentscope:resume")
            cancelled = await host.cancel(handle, idempotency_key="agentscope:cancel")
            return handle, events, paused, resumed, cancelled

    handle, events, paused, resumed, cancelled = asyncio.run(run())
    assert handle.task_id
    assert any(event["type"] in {"task.running", "task.updated"} for event in events)
    assert paused["status"] == "paused"
    assert resumed["status"] == "running"
    assert cancelled["status"] == "cancelled"


class FakeAgentScopeRuntime:
    """Offline host boundary: every runtime callback delegates to the injected bridge."""

    def __init__(self, host):
        self.host = host
        self.calls = []

    async def create(self, request):
        self.calls.append("create")
        return await self.host.create(request)

    async def subscribe(self, handle):
        self.calls.append("subscribe")
        return await self.host.subscribe(handle)

    async def pause(self, handle):
        self.calls.append("pause")
        return await self.host.pause(handle, idempotency_key="fake-runtime:pause")

    async def resume(self, handle):
        self.calls.append("resume")
        return await self.host.resume(handle, idempotency_key="fake-runtime:resume")

    async def cancel(self, handle):
        self.calls.append("cancel")
        return await self.host.cancel(handle, idempotency_key="fake-runtime:cancel")


def test_fake_agentscope_runtime_maps_full_lifecycle_without_network(tmp_path):
    app = create_app(tmp_path)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://valueroute") as client:
            bridge = AgentScopeHost(HttpxValueRouteApi(client))
            runtime = FakeAgentScopeRuntime(bridge)
            handle = await runtime.create(
                {
                    "tenant_id": "tenant",
                    "session_id": "fake-runtime",
                    "goal": "offline lifecycle",
                    "acceptance_contract": [{"id": "a", "description": "pass"}],
                    "workspace": {"canonical_uri": "workspace://fake", "base_revision": "r1"},
                    "orchestration_mode": "off",
                    "provider_id": "openai",
                    "model_id": "fake",
                    "reasoning_effort": "low",
                }
            )
            await runtime.subscribe(handle)
            paused = await runtime.pause(handle)
            resumed = await runtime.resume(handle)
            cancelled = await runtime.cancel(handle)
            return runtime.calls, paused, resumed, cancelled

    calls, paused, resumed, cancelled = asyncio.run(run())
    assert calls == ["create", "subscribe", "pause", "resume", "cancel"]
    assert [paused["status"], resumed["status"], cancelled["status"]] == ["paused", "running", "cancelled"]


def test_resume_and_cancel_do_not_report_success_for_wrong_state():
    class WrongStateApi:
        async def post(self, path, *, json, idempotency_key):
            action = path.rsplit("/", 1)[-1]
            return {"data": {"id": "t", "version": json["expected_version"] + 1, "status": "paused" if action == "resume" else "running"}}

        async def events(self, path, *, last_event_id=None):
            return []

    async def run():
        host = AgentScopeHost(WrongStateApi())
        handle = AgentScopeTaskHandle("t", 3)
        with pytest.raises(AgentScopeLifecycleError):
            await host.resume(handle, idempotency_key="wrong:resume")
        with pytest.raises(AgentScopeLifecycleError):
            await host.cancel(handle, idempotency_key="wrong:cancel")

    asyncio.run(run())
