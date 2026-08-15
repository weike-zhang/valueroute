from pathlib import Path

import pytest

from valueroute.domain.models import ResourceRegion, WriterLease
from valueroute.workspaces import ChangeSetRejected, IntegrationConflict, LocalWorkspaceAdapter


def lease(owner: str, revision: str, path: str, kind: str = "file") -> WriterLease:
    selector_type, selector_value = ("whole_resource", "") if kind == "file" else ("path_prefix", path)
    return WriterLease(
        id=f"lease-{owner}-{path}", child_task_id="child", owner_agent_id=owner,
        region=ResourceRegion(resource_kind=kind, resource_id=path if kind == "file" else "workspace", selector_type=selector_type, selector_value=selector_value, base_revision=revision),
    )


def test_owner_workspace_is_copied_and_changeset_has_actual_hashes_and_diff(tmp_path: Path):
    canonical = tmp_path / "canonical"
    canonical.mkdir(); (canonical / "app.py").write_text("old\n")
    adapter = LocalWorkspaceAdapter(canonical, tmp_path / "owners")
    snapshot = adapter.snapshot()
    owner = adapter.create_owner_workspace("worker-a", snapshot)
    (owner / "app.py").write_text("new\n")

    changeset = adapter.create_changeset("worker-a", owner, snapshot)

    assert (canonical / "app.py").read_text() == "old\n"
    assert changeset.files[0].before_hash != changeset.files[0].after_hash
    assert "-old" in changeset.files[0].diff and "+new" in changeset.files[0].diff


def test_scope_violation_rejects_the_whole_changeset_without_canonical_write(tmp_path: Path):
    canonical = tmp_path / "canonical"
    canonical.mkdir(); (canonical / "allowed.py").write_text("old\n"); (canonical / "outside.py").write_text("old\n")
    adapter = LocalWorkspaceAdapter(canonical, tmp_path / "owners")
    snapshot = adapter.snapshot(); owner = adapter.create_owner_workspace("worker-a", snapshot)
    (owner / "allowed.py").write_text("changed\n"); (owner / "outside.py").write_text("changed\n")
    changeset = adapter.create_changeset("worker-a", owner, snapshot)

    with pytest.raises(ChangeSetRejected, match="write_scope_violation"):
        adapter.integrate(changeset, [lease("worker-a", snapshot.revision, "allowed.py")])

    assert (canonical / "allowed.py").read_text() == "old\n"
    assert (canonical / "outside.py").read_text() == "old\n"


def test_directory_lease_integrates_and_base_conflict_does_not_pollute_canonical(tmp_path: Path):
    canonical = tmp_path / "canonical"
    canonical.mkdir(); (canonical / "src").mkdir(); (canonical / "src" / "app.py").write_text("old\n")
    adapter = LocalWorkspaceAdapter(canonical, tmp_path / "owners")
    snapshot = adapter.snapshot()
    first_owner = adapter.create_owner_workspace("worker-a", snapshot)
    second_owner = adapter.create_owner_workspace("worker-b", snapshot)
    (first_owner / "src" / "app.py").write_text("first\n")
    (second_owner / "src" / "app.py").write_text("second\n")
    first = adapter.create_changeset("worker-a", first_owner, snapshot)
    second = adapter.create_changeset("worker-b", second_owner, snapshot)

    adapter.integrate(first, [lease("worker-a", snapshot.revision, "src", kind="directory")])
    with pytest.raises(IntegrationConflict, match="base revision"):
        adapter.integrate(second, [lease("worker-b", snapshot.revision, "src", kind="directory")])

    assert (canonical / "src" / "app.py").read_text() == "first\n"


def test_parallel_disjoint_changesets_rebase_safely_after_first_integration(tmp_path: Path):
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    (canonical / "frontend.txt").write_text("old frontend\n")
    (canonical / "backend.txt").write_text("old backend\n")
    adapter = LocalWorkspaceAdapter(canonical, tmp_path / "owners")
    snapshot = adapter.snapshot()
    frontend = adapter.create_owner_workspace("frontend", snapshot)
    backend = adapter.create_owner_workspace("backend", snapshot)
    (frontend / "frontend.txt").write_text("new frontend\n")
    (backend / "backend.txt").write_text("new backend\n")
    frontend_changes = adapter.create_changeset("frontend", frontend, snapshot)
    backend_changes = adapter.create_changeset("backend", backend, snapshot)
    leases = [lease("frontend", snapshot.revision, "frontend.txt"), lease("backend", snapshot.revision, "backend.txt")]

    adapter.integrate(frontend_changes, leases)
    adapter.integrate(backend_changes, leases)

    assert (canonical / "frontend.txt").read_text() == "new frontend\n"
    assert (canonical / "backend.txt").read_text() == "new backend\n"
