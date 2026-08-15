"""Reproducible, offline NFR and supply-chain evidence.

This is an evidence collector, not a performance gate.  It intentionally uses
only the local filesystem, Python standard library, and the checked-out
ValueRoute package; it never downloads dependencies or calls a provider.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import platform
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((percentile / 100) * (len(ordered) - 1))))
    return ordered[index]


def _environment() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "cpu_count": str(os.cpu_count() or 1),
        "revision_hint": _revision_hint(),
    }


def _revision_hint() -> str:
    files = sorted(ROOT.glob("src/**/*.py"))
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def benchmark(iterations: int) -> dict[str, object]:
    from valueroute.storage.journal import LocalJournal

    latencies_ms: list[float] = []
    with tempfile.TemporaryDirectory(prefix="valueroute-nfr-") as directory:
        with LocalJournal(Path(directory)) as journal:
            started = time.perf_counter()
            for index in range(iterations):
                operation_started = time.perf_counter()
                journal.append_frame([{"type": "nfr.sample", "data": {"index": index}}])
                latencies_ms.append((time.perf_counter() - operation_started) * 1000)
            elapsed = time.perf_counter() - started
            replay_started = time.perf_counter()
            replay_count = len(journal.events())
            replay_ms = (time.perf_counter() - replay_started) * 1000
            journal.close()
    return {
        "kind": "local_journal_append_and_replay",
        "iterations": iterations,
        "elapsed_ms": round(elapsed * 1000, 3),
        "throughput_ops_per_second": round(iterations / elapsed, 3) if elapsed else None,
        "latency_ms": {
            "p50": round(_percentile(latencies_ms, 50), 3),
            "p95": round(_percentile(latencies_ms, 95), 3),
            "max": round(max(latencies_ms), 3),
        },
        "replay": {"records": replay_count, "elapsed_ms": round(replay_ms, 3)},
        "environment": _environment(),
        "interpretation": "Evidence for this local workload only; no SLO or production capacity claim.",
    }


def sbom() -> dict[str, object]:
    packages = []
    for distribution in metadata.distributions():
        name = distribution.metadata.get("Name")
        version = distribution.version
        if name and version:
            packages.append({"name": name, "version": version})
    packages.sort(key=lambda item: (item["name"].lower(), item["version"]))
    return {
        "bomFormat": "CycloneDX-compatible inventory",
        "specVersion": "1.5",
        "metadata": {"tool": "scripts/nfr_evidence.py", "environment": _environment()},
        "components": [{"type": "library", "name": p["name"], "version": p["version"]} for p in packages],
        "interpretation": "Offline installed-environment inventory; not a signed release SBOM and not a vulnerability scan.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("benchmark", "sbom"), nargs="?", default="benchmark")
    parser.add_argument("--iterations", type=int, default=200, help="local journal operations (default: 200)")
    parser.add_argument("--output", type=Path, help="write JSON evidence to this path")
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be positive")
    result = benchmark(args.iterations) if args.command == "benchmark" else sbom()
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
