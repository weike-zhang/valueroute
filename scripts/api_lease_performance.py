"""Small, repeatable local API and lease-conflict performance acceptance.

This is a local workload probe, not a production capacity claim. It uses the
checked-out application and an isolated temporary journal; it never calls a
provider or an external service.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def percentile(values: list[float], percentile_value: float) -> float:
    ordered = sorted(values)
    index = round((percentile_value / 100) * (len(ordered) - 1))
    return ordered[max(0, min(len(ordered) - 1, index))]


def latency_summary(values: list[float]) -> dict[str, float]:
    return {"p50_ms": round(percentile(values, 50), 3), "p95_ms": round(percentile(values, 95), 3), "max_ms": round(max(values), 3)}


def run(iterations: int) -> dict[str, object]:
    from fastapi.testclient import TestClient

    from valueroute.api.app import create_app
    from valueroute.domain.errors import DomainError
    from valueroute.domain.models import ResourceRegion
    from valueroute.ownership.manager import LeaseManager
    from valueroute.storage.journal import LocalJournal
    from valueroute.storage.store import Store

    with tempfile.TemporaryDirectory(prefix="valueroute-api-lease-") as directory:
        with TestClient(create_app(Path(directory) / "api")) as client:
            api_latencies: list[float] = []
            for _ in range(iterations):
                started = time.perf_counter()
                response = client.get("/v1/health/live")
                api_latencies.append((time.perf_counter() - started) * 1000)
                if response.status_code != 200 or response.json() != {"status": "ok"}:
                    raise RuntimeError(f"live API probe failed: {response.status_code} {response.text}")

        journal = LocalJournal(Path(directory) / "leases")
        try:
            store = Store(journal)
            manager = LeaseManager(store)
            region = ResourceRegion(resource_kind="file", resource_id="workspace/src/app.py", selector_type="whole_resource", selector_value="", base_revision="revision-1")
            manager.acquire("child-1", "owner-1", region, acquired_at=datetime.now(timezone.utc))
            conflict_latencies: list[float] = []
            conflicts = 0
            for index in range(iterations):
                started = time.perf_counter()
                try:
                    manager.acquire(f"child-{index + 2}", f"owner-{index + 2}", region)
                except DomainError as error:
                    if error.code != "lease_overlap":
                        raise
                    conflicts += 1
                else:
                    raise AssertionError("overlapping lease was accepted")
                conflict_latencies.append((time.perf_counter() - started) * 1000)
        finally:
            journal.close()

    return {
        "kind": "api_p95_and_lease_conflict",
        "iterations": iterations,
        "api": {"operation": "GET /v1/health/live", "latency": latency_summary(api_latencies)},
        "lease_conflict": {
            "operation": "LeaseManager.acquire(overlapping region)",
            "latency": latency_summary(conflict_latencies),
            "conflicts_rejected": conflicts,
            "all_conflicts_rejected": conflicts == iterations,
        },
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "interpretation": "Evidence for this local in-process workload only; no production SLO or capacity claim.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=200, help="samples per workload (default: 200)")
    parser.add_argument("--output", type=Path, help="write JSON evidence to this path")
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be positive")
    rendered = json.dumps(run(args.iterations), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
