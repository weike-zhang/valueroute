import subprocess
from pathlib import Path

import pytest

from valueroute.domain.models import ResourceRegion, WriterLease
from valueroute.workspaces import GitWorkspaceAdapter, GitWorkspaceError


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "ValueRoute Test")
    (root / "app.py").write_text("old\n")
    (root / "src").mkdir()
    (root / "src" / "keep.py").write_text("keep\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    return root


def test_git_owner_worktree_has_head_baseline_and_cannot_touch_canonical(tmp_path: Path):
    repo = make_repo(tmp_path)
    adapter = GitWorkspaceAdapter(repo, tmp_path / "owners")
    snapshot = adapter.snapshot()

    owner = adapter.bind_owner_workspace("worker-a", base_revision=snapshot.revision)
    assert owner.base_revision == git(repo, "rev-parse", "HEAD")
    assert (owner.path / "app.py").read_text() == "old\n"

    (owner.path / "app.py").write_text("new\n")
    (owner.path / "new.txt").write_text("created\n")
    changeset = adapter.create_changeset("worker-a", owner.path, snapshot)

    assert {change.path for change in changeset.files} == {"app.py", "new.txt"}
    assert any("-old" in change.diff and "+new" in change.diff for change in changeset.files)
    assert (repo / "app.py").read_text() == "old\n"
    assert not (repo / "new.txt").exists()

    adapter.cleanup_owner_workspace(owner.path)
    assert not owner.path.exists()
    assert str(owner.path) not in git(repo, "worktree", "list", "--porcelain")


def test_git_snapshot_rejects_dirty_canonical_and_stale_baseline(tmp_path: Path):
    repo = make_repo(tmp_path)
    adapter = GitWorkspaceAdapter(repo, tmp_path / "owners")
    revision = adapter.snapshot().revision
    (repo / "app.py").write_text("uncommitted\n")

    with pytest.raises(GitWorkspaceError, match="clean"):
        adapter.snapshot()
    with pytest.raises(GitWorkspaceError, match="clean"):
        adapter.bind_owner_workspace("worker-a", base_revision=revision)


def test_git_worktree_creation_failure_cleans_partial_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = make_repo(tmp_path)
    owners = tmp_path / "owners"
    adapter = GitWorkspaceAdapter(repo, owners)

    original_git = adapter._git

    def fail_worktree(args: list[str]) -> str:
        if args[:2] == ["worktree", "add"]:
            raise GitWorkspaceError("simulated Git failure")
        return original_git(args)

    monkeypatch.setattr(adapter, "_git", fail_worktree)
    with pytest.raises(GitWorkspaceError, match="failed to create"):
        adapter.bind_owner_workspace("worker-a")

    # A failed adapter operation must never leave a usable-looking owner path.
    assert list(owners.iterdir()) == []


def test_cleanup_rejects_arbitrary_path(tmp_path: Path):
    repo = make_repo(tmp_path)
    adapter = GitWorkspaceAdapter(repo, tmp_path / "owners")
    arbitrary = tmp_path / "owners" / "not-an-owner"
    arbitrary.mkdir()

    with pytest.raises(ValueError, match="adapter-created"):
        adapter.cleanup_owner_workspace(arbitrary)


def test_explicit_adoption_commits_integration_worktree_without_moving_canonical_head(tmp_path: Path):
    repo = make_repo(tmp_path)
    adapter = GitWorkspaceAdapter(repo, tmp_path / "owners")
    snapshot = adapter.snapshot()
    owner = adapter.bind_owner_workspace("worker-a", base_revision=snapshot.revision)
    (owner.path / "app.py").write_text("adopted\n")
    changeset = adapter.create_changeset("worker-a", owner.path, snapshot)
    lease = WriterLease(
        id="lease-app", child_task_id="child", owner_agent_id="worker-a",
        region=ResourceRegion(
            resource_kind="file", resource_id="app.py", selector_type="whole_resource",
            selector_value="", base_revision=snapshot.revision,
        ),
    )

    result = adapter.adopt_changeset(changeset, [lease], commit_message="adopt worker change")

    assert result.commit == result.revision
    assert result.commit != snapshot.revision
    assert git(repo, "rev-parse", "HEAD") == snapshot.revision
    assert (repo / "app.py").read_text() == "old\n"
    assert adapter._git(["show", f"{result.commit}:app.py"]) == "adopted"
    adapter.cleanup_owner_workspace(owner.path)
