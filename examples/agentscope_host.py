"""Minimal AgentScope host for the ValueRoute public lifecycle.

Run ``python examples/agentscope_host.py --check`` after installing the project.
The check is intentionally useful in environments where the optional AgentScope
package is absent: it exits successfully and prints an explicit SKIP message.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

try:
    from valueroute.frameworks.agentscope import AgentScopeHost, HttpxValueRouteApi, detect_agentscope
except ModuleNotFoundError:
    # Make the checked-in example runnable from a source checkout as well as an
    # editable install. This does not affect the installed package.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from valueroute.frameworks.agentscope import AgentScopeHost, HttpxValueRouteApi, detect_agentscope


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the minimal AgentScope-to-ValueRoute host bridge")
    parser.add_argument("--check", action="store_true", help="probe the optional AgentScope installation and exit")
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    parser.add_argument("--tenant-id", default="example-tenant")
    parser.add_argument("--session-id", default="agentscope-example")
    parser.add_argument("--goal", default="Run the AgentScope host lifecycle example")
    parser.add_argument("--action", choices=("none", "pause", "resume", "cancel"), default="none")
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    installation = detect_agentscope()
    if not installation.available:
        print(f"SKIP: optional AgentScope host is unavailable ({installation.reason}).")
        return 0
    if args.check:
        print(f"OK: AgentScope {installation.version} is importable.")
        return 0

    import httpx

    host_request = {
        "tenant_id": args.tenant_id,
        "session_id": args.session_id,
        "goal": args.goal,
        "acceptance_contract": [{"id": "host", "description": "host lifecycle is mapped"}],
        "workspace": {"canonical_uri": "workspace://agentscope-example", "base_revision": "example"},
        "orchestration_mode": "off",
        "provider_id": "openai",
        "model_id": "agentscope-host",
        "reasoning_effort": "low",
    }
    async with httpx.AsyncClient(base_url=args.base_url) as client:
        host = AgentScopeHost(HttpxValueRouteApi(client))
        handle = await host.create(host_request)
        events = await host.subscribe(handle)
        output: dict[str, object] = {"task_id": handle.task_id, "events": events}
        if args.action != "none":
            action = getattr(host, args.action)
            output[args.action] = await action(handle, idempotency_key=f"agentscope-example:{args.action}")
        print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    return asyncio.run(_run(_arguments()))


if __name__ == "__main__":
    raise SystemExit(main())
