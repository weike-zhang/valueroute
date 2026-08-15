# AgentScope host example

The checked-in host is [`examples/agentscope_host.py`](../examples/agentscope_host.py). It uses AgentScope 2.x as an optional host dependency and maps an AgentScope-style session through ValueRoute's public HTTP contract. The extra is version-bounded as `agentscope>=2.0,<3` and requires Python 3.11+.

Install and probe it from the repository root:

```bash
python3.11 -m pip install -e '.[agentscope]'
python3.11 examples/agentscope_host.py --check
```

If the optional package is not installed, the command exits successfully with an explicit `SKIP` message. This is intentional: the dependency-free adapter and its ASGI contract tests remain runnable in the base development environment.

```python
import httpx
from valueroute.frameworks.agentscope import AgentScopeHost, HttpxValueRouteApi

async with httpx.AsyncClient(base_url="http://127.0.0.1:8787") as client:
    host = AgentScopeHost(HttpxValueRouteApi(client))
    handle = await host.create(host_request)
    events = await host.subscribe(handle)
    paused = await host.pause(handle, idempotency_key="agentscope:pause")
    resumed = await host.resume(handle, idempotency_key="agentscope:resume")
    cancelled = await host.cancel(handle, idempotency_key="agentscope:cancel")
```

The contract test uses `httpx.ASGITransport` to exercise the same `create → subscribe → pause → resume → cancel` lifecycle against the FastAPI app. `detect_agentscope()` probes both distribution metadata and importability, so a missing or broken optional installation is reported rather than turned into an import-time failure.
