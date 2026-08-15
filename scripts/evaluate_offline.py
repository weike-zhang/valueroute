"""Run the offline evaluation set against a live OpenAI-compatible endpoint.

The frozen task set (``evaluation/frozen_tasks.json``) encodes three task
families from design section 20.2.  For every task this harness:

1. builds a ``RoutingRequestEnvelope`` and runs the advisory pipeline, so the
   suggested delegation (direct vs workers) is recorded against ground truth;
2. executes the task with a real model in three configurations:
   - A: fixed host controller, zero workers (single-agent baseline);
   - B: fixed host controller, one fixed worker;
   - C: fixed host controller, adaptive 0-5 workers per the advisory result;
3. measures tokens, wall time, and estimated cost per configuration and checks
   each result against the task acceptance criteria.

The output JSON is audit evidence.  It records the endpoint host, model id,
code fingerprint, price table, frozen tasks, raw per-configuration results,
and an honest interpretation.  It never claims to certify model quality beyond
what the acceptance criteria measure.

The API key is read from the environment (``VALUEROUTE_EVAL_API_KEY``) or
``--api-key``; it is never written into the output JSON or any repository file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DEFAULT_PRICE = {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 4.0}

CHAT_SYSTEM = (
    "You are ValueRoute's evaluation worker. Complete the assigned change "
    "precisely. State the root cause, the exact files you changed, and how you "
    "verified the result. Do not invent files or credentials."
)


def fingerprint() -> str:
    digest = hashlib.sha256()
    for path in sorted((ROOT / "src").rglob("*.py")):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def environment() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "revision_hint": fingerprint()[:16],
    }


def load_tasks(path: Path) -> list[dict[str, object]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != "v1":
        raise ValueError(f"unsupported frozen-task schema: {data.get('schema_version')}")
    tasks = []
    for family in data["families"]:
        for task in family["tasks"]:
            task["family_id"] = family["id"]
            task["family_label"] = family["label"]
            if "default_write_regions" not in task:
                task["default_write_regions"] = family.get("default_write_regions") or []
            tasks.append(task)
    return tasks


def build_envelope(task: dict[str, object]) -> dict[str, object]:
    from valueroute.routing.models import (
        RoutingPermissions,
        RoutingRequestEnvelope,
        RoutingResourceSummary,
    )

    regions = task.get("default_write_regions") or []
    envelope = {
        "id": f"env-{task['id']}",
        "tenant_id": "eval_tenant",
        "host_session_id": f"sess-{task['id']}",
        "user_text": task["user_text"],
        "permissions": RoutingPermissions(requested_write_regions=regions).model_dump(mode="json"),
        "resource_summary": RoutingResourceSummary(
            canonical_uri=f"file:///repos/valueroute/{task['family_id']}",
            base_revision="0000000",
            referenced_paths=sorted({r["selector_value"] for r in regions}),
        ).model_dump(mode="json"),
        "data_classification": "internal",
    }
    return RoutingRequestEnvelope.model_validate(envelope).model_dump(mode="json")


def advisory_decision(task: dict[str, object]) -> dict[str, object]:
    from valueroute.routing.advisory import AdvisoryEngine
    from valueroute.routing.boundary import classify_boundary
    from valueroute.routing.models import RoutingRequestEnvelope
    from valueroute.routing.profiler import Profiler

    envelope = RoutingRequestEnvelope.model_validate(build_envelope(task))
    boundary = classify_boundary(envelope)
    graph = Profiler().profile(envelope)
    advice = AdvisoryEngine().advise(envelope, boundary, graph)
    workers = [c for c in advice.candidates if c.mode == "workers"]
    suggested_worker_count = 0
    if workers and not workers[0].rejection_codes:
        suggested_worker_count = workers[0].worker_count
    suggestion = "direct" if suggested_worker_count == 0 else "workers"
    return {
        "request_type": boundary.request_type,
        "boundary_method": boundary.method,
        "suggestion": suggestion,
        "suggested_worker_count": suggested_worker_count,
        "rejection_codes": workers[0].rejection_codes if workers else [],
        "basis_version": advice.requirement_graph.profiler_version,
        "rationale": [c.rationale for c in advice.candidates],
    }


def classify(suggestion: str, ground: dict[str, object]) -> str:
    """Return one of correct / over_delegated / under_delegated for this task."""
    expected = "workers" if ground.get("delegation") == "workers" else "direct"
    if suggestion == expected:
        return "correct"
    if suggestion == "workers":
        return "over_delegated"
    return "under_delegated"


async def call_chat(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    api_key: str,
    model: str,
    user_text: str,
    max_tokens: int = 1024,
) -> dict[str, object]:
    started = time.perf_counter()
    response = await client.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": CHAT_SYSTEM},
                {"role": "user", "content": user_text},
            ],
            "max_tokens": max_tokens,
        },
        timeout=httpx.Timeout(300.0),
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    if response.status_code >= 400:
        raise RuntimeError(f"provider returned HTTP {response.status_code}: {response.text[:300]}")
    body = response.json()
    message = (body.get("choices") or [{}])[0].get("message") or {}
    usage = body.get("usage") or {}
    text = message.get("content") or ""
    return {
        "text": text,
        "latency_ms": elapsed_ms,
        "usage": {
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        },
        "http_status": response.status_code,
    }


def estimate_cost(usage: dict[str, object], price: dict[str, float]) -> float | None:
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if input_tokens is None or output_tokens is None:
        return None
    return round(
        (input_tokens / 1_000_000) * price["input_usd_per_mtok"]
        + (output_tokens / 1_000_000) * price["output_usd_per_mtok"],
        6,
    )


def _execution_prompt(task: dict[str, object], role: str) -> str:
    return (
        f"[role: {role}]\n"
        f"Task ({task['family_label']} / {task['id']}): {task['description']}\n"
        f"User request: {task['user_text']}\n"
        f"Ground truth delegation expectation is NOT to be revealed; complete the task as asked."
    )


def acceptance_met(task: dict[str, object], text: str) -> bool:
    """A conservative keyword acceptance check against the task description."""
    keywords = [
        token
        for token in (task.get("acceptance_keywords") or task["description"].split())
        if len(token) > 3
    ]
    if not keywords:
        return True
    lowered = text.lower()
    matched = sum(1 for keyword in keywords if keyword.lower() in lowered)
    return matched >= max(1, len(keywords) // 2)


async def run_config(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    api_key: str,
    model: str,
    task: dict[str, object],
    config: str,
    worker_count: int,
    price: dict[str, float],
) -> dict[str, object]:
    calls: list[dict[str, object]] = []
    started = time.perf_counter()
    if config == "A":
        calls.append(await call_chat(client, base_url=base_url, api_key=api_key, model=model, user_text=_execution_prompt(task, "host-controller")))
    elif config == "B":
        calls.append(await call_chat(client, base_url=base_url, api_key=api_key, model=model, user_text=_execution_prompt(task, "host-controller")))
        calls.append(await call_chat(client, base_url=base_url, api_key=api_key, model=model, user_text=_execution_prompt(task, "fixed-worker")))
    else:
        count = max(1, worker_count)
        calls.append(await call_chat(client, base_url=base_url, api_key=api_key, model=model, user_text=_execution_prompt(task, "host-controller")))
        for index in range(count):
            calls.append(await call_chat(client, base_url=base_url, api_key=api_key, model=model, user_text=_execution_prompt(task, f"adaptive-worker-{index + 1}")))  # noqa: PERF401

    combined_text = "\n".join(str(call.get("text", "")) for call in calls)
    total_latency = sum(int(call["latency_ms"]) for call in calls)
    input_tokens = sum((call["usage"].get("input_tokens") or 0) for call in calls)
    output_tokens = sum((call["usage"].get("output_tokens") or 0) for call in calls)
    estimated_cost = sum(estimate_cost(call["usage"], price) or 0.0 for call in calls)
    return {
        "config": config,
        "worker_count": worker_count,
        "calls": len(calls),
        "passed": acceptance_met(task, combined_text),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "latency_ms": total_latency,
        "wall_ms": int((time.perf_counter() - started) * 1000),
        "estimated_cost_usd": round(estimated_cost, 6),
        "text_excerpt": combined_text[:400],
        "per_call": [
            {"latency_ms": call["latency_ms"], "usage": call["usage"], "http_status": call["http_status"]}
            for call in calls
        ],
    }


async def run_tasks(
    *,
    base_url: str,
    api_key: str,
    model: str,
    tasks: list[dict[str, object]],
    price: dict[str, float],
    skip_live: bool,
) -> list[dict[str, object]]:
    results = []
    async with httpx.AsyncClient() as client:
        for task in tasks:
            decision = advisory_decision(task)
            ground = task["ground_truth"]
            configs = []
            if not skip_live:
                for config, worker_count in (("A", 0), ("B", 1), ("C", decision["suggested_worker_count"])):
                    try:
                        configs.append(await run_config(client, base_url=base_url, api_key=api_key, model=model, task=task, config=config, worker_count=worker_count, price=price))
                    except Exception as error:
                        configs.append({"config": config, "worker_count": worker_count, "error": str(error), "passed": False})
            results.append(
                {
                    "task_id": task["id"],
                    "family_id": task["family_id"],
                    "description": task["description"],
                    "ground_truth": ground,
                    "advisory": decision,
                    "delegation_verdict": classify(decision["suggestion"], ground),
                    "configs": configs,
                }
            )
    return results


def aggregate(results: list[dict[str, object]]) -> dict[str, object]:
    verdicts = [str(item["delegation_verdict"]) for item in results]
    total = len(verdicts)
    correct = verdicts.count("correct")
    over = verdicts.count("over_delegated")
    under = verdicts.count("under_delegated")
    per_config: dict[str, dict[str, object]] = {}
    for item in results:
        for config in item["configs"]:
            key = str(config.get("config"))
            bucket = per_config.setdefault(
                key,
                {"count": 0, "passed": 0, "total_tokens": 0, "latency_ms": 0, "wall_ms": 0, "cost_usd": 0.0},
            )
            if "error" in config:
                bucket["count"] += 1
                continue
            bucket["count"] += 1
            bucket["passed"] += 1 if config.get("passed") else 0
            bucket["total_tokens"] += int(config.get("total_tokens") or 0)
            bucket["latency_ms"] += int(config.get("latency_ms") or 0)
            bucket["wall_ms"] += int(config.get("wall_ms") or 0)
            bucket["cost_usd"] = round(float(bucket["cost_usd"]) + float(config.get("estimated_cost_usd") or 0), 6)
    return {
        "task_count": total,
        "delegation": {
            "correct_rate": round(correct / total, 4) if total else None,
            "over_delegated_rate": round(over / total, 4) if total else None,
            "under_delegated_rate": round(under / total, 4) if total else None,
            "correct": correct,
            "over_delegated": over,
            "under_delegated": under,
        },
        "per_config": per_config,
    }


async def main_async(args: argparse.Namespace) -> dict[str, object]:
    api_key = args.api_key or os.getenv("VALUEROUTE_EVAL_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("no API key: set VALUEROUTE_EVAL_API_KEY or pass --api-key")
    tasks = load_tasks(args.tasks)
    price = {"input_usd_per_mtok": args.input_price, "output_usd_per_mtok": args.output_price}
    results = await run_tasks(
        base_url=args.base_url,
        api_key=api_key,
        model=args.model,
        tasks=tasks,
        price=price,
        skip_live=args.skip_live,
    )
    return {
        "schema_version": "offline-evaluation-1",
        "evaluation_mode": "live_model" if not args.skip_live else "advisory_only",
        "quality_claim": False,
        "endpoint_host": args.base_url.split("://")[1].split("/")[0],
        "model_id": args.model,
        "price_table": price,
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "code_fingerprint": fingerprint(),
        "environment": environment(),
        "task_count": len(results),
        "results": results,
        "aggregate": aggregate(results),
        "interpretation": (
            "Offline evaluation evidence: advisory routing decisions are compared "
            "against frozen ground truth, and A/B/C configurations are measured on a "
            "live model for cost/latency/pass. quality_claim is False: acceptance is "
            "keyword-based, not full task execution; no production or model-quality "
            "certification is implied."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://186.241.75.230:3000/v1")
    parser.add_argument("--model", default="gpt-5-6-mini")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--tasks", type=Path, default=ROOT / "evaluation/frozen_tasks.json")
    parser.add_argument("--input-price", type=float, default=DEFAULT_PRICE["input_usd_per_mtok"])
    parser.add_argument("--output-price", type=float, default=DEFAULT_PRICE["output_usd_per_mtok"])
    parser.add_argument("--skip-live", action="store_true", help="only run advisory decisions, skip live model calls")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    import asyncio

    result = asyncio.run(main_async(args))
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
