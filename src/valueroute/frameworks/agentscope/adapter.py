from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Protocol


class ValueRouteApi(Protocol):
    async def post(self, path: str, *, json: dict[str, Any], idempotency_key: str) -> dict[str, Any]: ...

    async def events(self, path: str, *, last_event_id: str | None = None) -> list[dict[str, Any]]: ...


class AgentScopeLifecycleError(RuntimeError):
    """Raised when a successful HTTP response does not prove the requested action."""


class HttpxValueRouteApi:
    """Small injected-client bridge; importing httpx is left to the host application."""

    def __init__(self, client: Any, *, base_url: str = ""):
        self.client = client
        self.base_url = base_url.rstrip("/")

    async def post(self, path: str, *, json: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        response = await self.client.post(f"{self.base_url}{path}", json=json, headers={"Idempotency-Key": idempotency_key})
        response.raise_for_status()
        return response.json()

    async def events(self, path: str, *, last_event_id: str | None = None) -> list[dict[str, Any]]:
        headers = {"Last-Event-ID": last_event_id} if last_event_id is not None else {}
        response = await self.client.get(f"{self.base_url}{path}", headers=headers)
        response.raise_for_status()
        return _parse_sse(response.text)


@dataclass(frozen=True)
class AgentScopeInstallation:
    """Runtime probe result for the optional AgentScope host dependency."""

    available: bool
    version: str | None = None
    reason: str | None = None


def detect_agentscope() -> AgentScopeInstallation:
    """Detect the installed optional package without making it import-time required."""
    try:
        installed_version = version("agentscope")
    except PackageNotFoundError:
        return AgentScopeInstallation(False, reason="install valueroute[agentscope] (Python 3.11+)")
    try:
        import_module("agentscope")
    except Exception as exc:  # pragma: no cover - depends on the host installation
        return AgentScopeInstallation(False, installed_version, reason=f"agentscope import failed: {exc}")
    return AgentScopeInstallation(True, installed_version)


def _parse_sse(body: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    data_lines: list[str] = []
    for line in [*body.splitlines(), ""]:
        if line == "":
            if current or data_lines:
                import json

                value: Any = "\n".join(data_lines)
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    pass
                if isinstance(value, dict) and "type" in value and "data" in value:
                    # ValueRoute's SSE frame carries the normalized event as
                    # JSON data; do not wrap that event a second time.
                    event = dict(value)
                    if "id" not in event and "id" in current:
                        event["id"] = current["id"]
                    events.append(event)
                else:
                    current["data"] = value
                    events.append(current)
            current, data_lines = {}, []
            continue
        field, _, value = line.partition(":")
        value = value.lstrip()
        if field == "data":
            data_lines.append(value)
        elif field in {"id", "event"}:
            current["id" if field == "id" else "type"] = value
    return events


class AgentScopeFrameworkAdapter:
    """Narrow, dependency-free mapping between AgentScope-shaped data and ValueRoute API data."""

    def session_payload(self, host: dict[str, Any]) -> dict[str, Any]:
        return {"tenant_id": host["tenant_id"], "host_session_id": host["session_id"], "orchestration_mode": host.get("orchestration_mode", "worker_only")}

    def task_payload(self, host: dict[str, Any], session_id: str) -> dict[str, Any]:
        return {
            "controller_session_id": session_id,
            "request_type": host.get("request_type", "new_task"),
            "goal": host["goal"],
            "acceptance_contract": host["acceptance_contract"],
            "data_classification": host.get("data_classification", "internal"),
            "workspace": host["workspace"],
            "budgets": host.get("budgets", {}),
        }

    def event_to_host(self, event: dict[str, Any]) -> dict[str, Any]:
        return {"event_id": event.get("id"), "sequence": event.get("sequence"), "type": event.get("type"), "data": event.get("data", {})}

    async def start_task(self, api: ValueRouteApi, host: dict[str, Any]) -> dict[str, Any]:
        """Drive the host's create/plan/execute lifecycle through the public API contract."""
        session_key = f"agentscope:{host['session_id']}:session"
        session = await api.post("/v1/controller-sessions", json=self.session_payload(host), idempotency_key=session_key)
        session_data = session["data"]
        epoch = await api.post(
            f"/v1/controller-sessions/{session_data['id']}/epochs",
            json={"expected_version": session_data["version"], "provider_id": host.get("provider_id", "openai"), "model_id": host.get("model_id", ""), "reasoning_effort": host.get("reasoning_effort", "medium")},
            idempotency_key=f"agentscope:{host['session_id']}:epoch",
        )
        task = await api.post("/v1/tasks", json=self.task_payload(host, session_data["id"]), idempotency_key=f"agentscope:{host['session_id']}:task")
        task_data = task["data"]
        plan_response: dict[str, Any] | None = None
        if host.get("plan") is not None:
            plan = dict(host["plan"])
            plan["expected_parent_version"] = task_data["version"]
            plan_response = await api.post(f"/v1/tasks/{task_data['id']}/plan", json=plan, idempotency_key=f"agentscope:{host['session_id']}:plan")
            task_data = {**task_data, "version": plan_response["meta"]["resource_version"]}
        executed = await api.post(f"/v1/tasks/{task_data['id']}/execute", json={"expected_version": task_data["version"]}, idempotency_key=f"agentscope:{host['session_id']}:execute")
        return {"session": session_data, "epoch": epoch["data"], "task": executed["data"], "plan": plan_response["data"] if plan_response else None}

    async def control(self, api: ValueRouteApi, task_id: str, action: str, expected_version: int, *, idempotency_key: str) -> dict[str, Any]:
        return await api.post(f"/v1/tasks/{task_id}/{action}", json={"expected_version": expected_version}, idempotency_key=idempotency_key)

    async def resume_events(self, api: ValueRouteApi, task_id: str, *, last_event_id: str | None = None) -> list[dict[str, Any]]:
        events = await api.events(f"/v1/tasks/{task_id}/events", last_event_id=last_event_id)
        return [self.event_to_host(event) for event in events]


@dataclass
class AgentScopeTaskHandle:
    """Mutable host-side cursor used to send expected-version controls safely."""

    task_id: str
    version: int


class AgentScopeHost:
    """Minimal executable host facade for AgentScope-style sessions.

    This class deliberately owns only the mapping between a host and ValueRoute's
    public API. It does not create a second execution engine or alter API responses.
    """

    def __init__(self, api: ValueRouteApi, *, adapter: AgentScopeFrameworkAdapter | None = None):
        self.api = api
        self.adapter = adapter or AgentScopeFrameworkAdapter()

    async def create(self, host: dict[str, Any]) -> AgentScopeTaskHandle:
        started = await self.adapter.start_task(self.api, host)
        task = started["task"]
        return AgentScopeTaskHandle(task_id=task["id"], version=task["version"])

    async def subscribe(self, handle: AgentScopeTaskHandle, *, last_event_id: str | None = None) -> list[dict[str, Any]]:
        return await self.adapter.resume_events(self.api, handle.task_id, last_event_id=last_event_id)

    async def _control(self, handle: AgentScopeTaskHandle, action: str, *, idempotency_key: str) -> dict[str, Any]:
        response = await self.adapter.control(
            self.api,
            handle.task_id,
            action,
            handle.version,
            idempotency_key=idempotency_key,
        )
        data = response["data"]
        expected_status = {"pause": "paused", "resume": "running", "cancel": "cancelled"}[action]
        if data.get("status") != expected_status:
            raise AgentScopeLifecycleError(
                f"ValueRoute acknowledged {action!r} without reaching {expected_status!r} "
                f"(actual={data.get('status')!r})"
            )
        handle.version = data["version"]
        return data

    async def pause(self, handle: AgentScopeTaskHandle, *, idempotency_key: str) -> dict[str, Any]:
        return await self._control(handle, "pause", idempotency_key=idempotency_key)

    async def resume(self, handle: AgentScopeTaskHandle, *, idempotency_key: str) -> dict[str, Any]:
        return await self._control(handle, "resume", idempotency_key=idempotency_key)

    async def cancel(self, handle: AgentScopeTaskHandle, *, idempotency_key: str) -> dict[str, Any]:
        return await self._control(handle, "cancel", idempotency_key=idempotency_key)
