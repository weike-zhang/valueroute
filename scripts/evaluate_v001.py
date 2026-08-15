"""Run the frozen, deterministic v0.0.1 orchestration evaluation.

This harness measures ValueRoute's local coordination invariants, not model
quality.  It intentionally uses deterministic task fixtures so the raw JSON
can be reviewed without credentials or network access.  A provider/model
identity is still recorded, and a real-model evaluation must replace the
fixture executor before making quality, cost, or latency claims.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@dataclass(frozen=True)
class FrozenTask:
    task_id: str
    family: str
    description: str
    expected_invariant: str


TASKS = (
    FrozenTask("independent-files", "independent_file_changes", "two disjoint files", "both changes integrate"),
    FrozenTask("overlap-serialize", "overlapping_changes", "same file from two owners", "one stale changeset is rejected without pollution"),
    FrozenTask("recovery-replay", "recovery_after_interruption", "durable running attempt", "restart replay retains the attempt and recovery facts"),
)


def fingerprint() -> str:
    digest = hashlib.sha256()
    for path in sorted((ROOT / "src").rglob("*.py")):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _independent_files(root: Path) -> dict[str, object]:
    from valueroute.domain.models import ResourceRegion
    from valueroute.ownership.manager import LeaseManager
    from valueroute.storage.journal import LocalJournal
    from valueroute.storage.store import Store
    from valueroute.workspaces.local import LocalWorkspaceAdapter

    canonical = root / "canonical"
    canonical.mkdir(parents=True)
    (canonical / "frontend.txt").write_text("old frontend\n", encoding="utf-8")
    (canonical / "backend.txt").write_text("old backend\n", encoding="utf-8")
    adapter = LocalWorkspaceAdapter(canonical, root / "workers")
    snapshot = adapter.snapshot()
    journal = LocalJournal(root / "state")
    try:
        store = Store(journal)
        leases = []
        workspaces = []
        for owner, path_name, content in (("frontend-owner", "frontend.txt", "new frontend\n"), ("backend-owner", "backend.txt", "new backend\n")):
            workspace = adapter.create_owner_workspace(owner, snapshot)
            workspaces.append(workspace)
            adapter.write_file(workspace, path_name, content.encode())
            region = ResourceRegion(resource_kind="file", resource_id=path_name, selector_type="whole_resource", selector_value="", base_revision=snapshot.revision)
            leases.append(LeaseManager(store).acquire(owner, owner, region))
        changesets = {
            lease.owner_agent_id: adapter.create_changeset(lease.owner_agent_id, workspaces[i], snapshot)
            for i, lease in enumerate(leases)
        }
        from valueroute.integration.service import IntegrationService
        results = IntegrationService(adapter).integrate_in_order(["frontend-owner", "backend-owner"], changesets, leases)
        passed = all(item["status"] == "integrated" for item in results)
        return {"policy": "worker", "results": results, "quality_pass": passed}
    finally:
        for workspace in locals().get("workspaces", []):
            adapter.cleanup_owner_workspace(workspace)
        journal.close()


def _overlap_serialize(root: Path) -> dict[str, object]:
    from valueroute.domain.models import ResourceRegion
    from valueroute.ownership.manager import LeaseManager
    from valueroute.storage.journal import LocalJournal
    from valueroute.storage.store import Store
    from valueroute.workspaces.local import ChangeSetRejected, IntegrationConflict, LocalWorkspaceAdapter

    canonical = root / "canonical"
    canonical.mkdir(parents=True)
    (canonical / "shared.txt").write_text("base\n", encoding="utf-8")
    adapter = LocalWorkspaceAdapter(canonical, root / "workers")
    snapshot = adapter.snapshot()
    journal = LocalJournal(root / "state")
    workspaces = []
    try:
        store = Store(journal)
        manager = LeaseManager(store)
        region = ResourceRegion(resource_kind="file", resource_id="shared.txt", selector_type="whole_resource", selector_value="", base_revision=snapshot.revision)
        first = manager.acquire("owner-a", "owner-a", region)
        first_workspace = adapter.create_owner_workspace("owner-a", snapshot)
        workspaces.append(first_workspace)
        second_workspace = adapter.create_owner_workspace("owner-b", snapshot)
        workspaces.append(second_workspace)
        adapter.write_file(first_workspace, "shared.txt", b"first\n")
        first_changeset = adapter.create_changeset("owner-a", first_workspace, snapshot)
        adapter.integrate(first_changeset, [first])
        adapter.write_file(second_workspace, "shared.txt", b"second\n")
        second_changeset = adapter.create_changeset("owner-b", second_workspace, snapshot)
        try:
            adapter.integrate(second_changeset, [first])
        except (ChangeSetRejected, IntegrationConflict):
            rejected = True
        else:
            rejected = False
        passed = rejected and (canonical / "shared.txt").read_text(encoding="utf-8") == "first\n"
        return {"policy": "serialized", "first_integrated": True, "second_rejected": rejected, "quality_pass": passed}
    finally:
        for workspace in workspaces:
            adapter.cleanup_owner_workspace(workspace)
        journal.close()


def _recovery_replay(root: Path) -> dict[str, object]:
    from valueroute.domain.models import WorkerAttempt, WorkerAttemptStatus
    from valueroute.storage.journal import LocalJournal
    from valueroute.storage.store import Store

    journal = LocalJournal(root / "state")
    try:
        store = Store(journal)
        attempt = WorkerAttempt(id="attempt-recovery", worker_session_id="session-recovery", child_task_id="child-recovery", status=WorkerAttemptStatus.running)
        store.attempts[attempt.id] = attempt
        store.commit({"type": "worker.claimed", "data": attempt.model_dump(mode="json")})
        journal.close()
        replayed = Store(LocalJournal(root / "state"))
        retained = replayed.attempts.get(attempt.id)
        passed = retained is not None and retained.status is WorkerAttemptStatus.running
        replayed.journal.close()
        return {"policy": "recovery", "attempt_id": attempt.id, "replayed_status": retained.status.value if retained else None, "quality_pass": passed}
    finally:
        if getattr(journal, "_lock", None) is not None:
            journal.close()


def run(*, provider_id: str, model_id: str, trials: int) -> dict[str, object]:
    if trials < 1:
        raise ValueError("trials must be positive")
    raw_trials = []
    for trial in range(1, trials + 1):
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix=f"valueroute-eval-{trial}-") as directory:
            root = Path(directory)
            results = {
                "independent_file_changes": _independent_files(root / "independent"),
                "overlapping_changes": _overlap_serialize(root / "overlap"),
                "recovery_after_interruption": _recovery_replay(root / "recovery"),
            }
        raw_trials.append({"trial": trial, "elapsed_ms": round((time.perf_counter() - started) * 1000, 3), "tasks": results})
    return {
        "schema_version": "v001-evaluation-1",
        "evaluation_mode": "deterministic_fixture",
        "quality_claim": False,
        "provider_id": provider_id,
        "model_id": model_id,
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "code_fingerprint": fingerprint(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "frozen_tasks": [asdict(task) for task in TASKS],
        "trials": raw_trials,
        "all_invariants_pass": all(
            task["quality_pass"]
            for trial in raw_trials
            for task in trial["tasks"].values()
        ),
        "interpretation": "Local coordination evidence only; replace deterministic fixtures with a credentialed provider before making model-quality, cost, or production-latency claims.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-id", default="fixture")
    parser.add_argument("--model-id", default="deterministic-fixture")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = json.dumps(run(provider_id=args.provider_id, model_id=args.model_id, trials=args.trials), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result, encoding="utf-8")
    print(result, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
