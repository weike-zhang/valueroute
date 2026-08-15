"""Git worktree implementation of the owner workspace boundary.

This adapter deliberately keeps the canonical checkout read-only from the
worker's point of view.  A worker gets a detached worktree at one immutable
revision; its changes are read back into a ChangeSet and the worktree is
removed explicitly by the host.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import mkdtemp
from typing import Iterable

from valueroute.domain.models import ResourceRegion, WriterLease

from .local import (
    ChangeSet,
    ChangeSetRejected,
    FileChange,
    IntegrationConflict,
    OwnerWorkspace,
    WorkspaceSnapshot,
    _diff,
    _hash,
    _normal_path,
    _revision,
)


class GitWorkspaceError(RuntimeError):
    """A Git operation could not establish or safely remove isolation."""


@dataclass(frozen=True)
class AdoptionResult:
    """The commit created by an explicit host-owned ChangeSet adoption."""

    commit: str
    revision: str


class GitWorkspaceAdapter:
    """Create owner worktrees from a clean canonical Git checkout.

    The adapter refuses a dirty canonical checkout: silently omitting or
    copying uncommitted files would make the recorded base revision untruthful.
    """

    def __init__(self, canonical_root: Path | str, workspace_root: Path | str):
        self.canonical_root = Path(canonical_root).resolve()
        self.workspace_root = Path(workspace_root).resolve()
        if not self.canonical_root.is_dir():
            raise ValueError("canonical_root must be an existing directory")
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self._owners: dict[Path, str] = {}
        self._git(["rev-parse", "--is-inside-work-tree"])

    def snapshot(self) -> WorkspaceSnapshot:
        self._require_clean_canonical()
        revision = self._git(["rev-parse", "HEAD"])
        files: dict[str, bytes] = {}
        for relative in self._git(["ls-tree", "-r", "--name-only", revision]).splitlines():
            relative = _normal_path(relative)
            files[relative] = self._git_bytes(["show", f"{revision}:{relative}"])
        return WorkspaceSnapshot(revision=revision, files=files)

    def bind_owner_workspace(self, owner_id: str, *, base_revision: str | None = None) -> OwnerWorkspace:
        snapshot = self.snapshot()
        if base_revision is not None and base_revision != snapshot.revision:
            raise IntegrationConflict("canonical workspace revision does not match task base revision")
        target = Path(mkdtemp(prefix=f"{self._owner_name(owner_id)}-", dir=self.workspace_root))
        shutil.rmtree(target)
        try:
            self._git(["worktree", "add", "--detach", str(target), snapshot.revision])
        except BaseException as exc:
            # The target was created by this call, so it is safe to remove it.
            shutil.rmtree(target, ignore_errors=True)
            raise GitWorkspaceError(f"failed to create owner worktree: {exc}") from exc
        self._owners[target] = owner_id
        return OwnerWorkspace(owner_id=owner_id, path=target, base_revision=snapshot.revision)

    def create_owner_workspace(self, owner_id: str, snapshot: WorkspaceSnapshot) -> Path:
        return self.bind_owner_workspace(owner_id, base_revision=snapshot.revision).path

    def create_changeset(self, owner_id: str, owner_workspace: Path | str, snapshot: WorkspaceSnapshot) -> ChangeSet:
        root = self._owner_root(owner_id, owner_workspace)
        current = _worktree_files(root)
        files = tuple(
            FileChange(
                path=path,
                before_hash=_hash(snapshot.files.get(path)),
                after_hash=_hash(current.get(path)),
                diff=_diff(path, snapshot.files.get(path), current.get(path)),
                after_content=current.get(path),
            )
            for path in sorted(set(snapshot.files) | set(current))
            if snapshot.files.get(path) != current.get(path)
        )
        return ChangeSet(owner_id=owner_id, base_revision=snapshot.revision, files=files)

    def cleanup_owner_workspace(self, owner_workspace: Path | str) -> None:
        root = Path(owner_workspace).resolve()
        if root.parent != self.workspace_root or root not in self._owners:
            raise ValueError("owner_workspace must be an adapter-created Git worktree")
        try:
            self._git(["worktree", "remove", "--force", str(root)])
        except BaseException as exc:
            raise GitWorkspaceError(f"failed to remove owner worktree: {exc}") from exc
        finally:
            self._owners.pop(root, None)
            if root.exists():
                raise GitWorkspaceError("Git reported removal but owner worktree still exists")

    def validate_changeset(self, changeset: ChangeSet, leases: Iterable[WriterLease]) -> None:
        active = [lease for lease in leases if lease.status == "active" and lease.owner_agent_id == changeset.owner_id]
        for change in changeset.files:
            if not any(_allows(change.path, lease.region, changeset.base_revision) for lease in active):
                raise ChangeSetRejected(f"write_scope_violation: {change.path}")

    def integrate(self, changeset: ChangeSet, leases: Iterable[WriterLease]) -> WorkspaceSnapshot:
        """Reject implicit canonical writes; integration needs an explicit host boundary."""
        self.validate_changeset(changeset, leases)
        raise GitWorkspaceError("Git owner worktrees do not write the canonical checkout implicitly")

    def adopt_changeset(
        self,
        changeset: ChangeSet,
        leases: Iterable[WriterLease],
        *,
        commit_message: str | None = None,
    ) -> AdoptionResult:
        """Explicitly apply a validated ChangeSet in a disposable integration worktree.

        This is the host's opt-in adoption boundary.  The canonical checkout is
        never used as the write target and its HEAD/ref is never moved here.
        Any validation, base-revision, apply, or commit failure removes the
        temporary worktree and leaves the canonical revision untouched.
        """
        self.validate_changeset(changeset, leases)
        self._require_clean_canonical()
        current_revision = self._git(["rev-parse", "HEAD"])
        if current_revision != changeset.base_revision:
            raise IntegrationConflict("canonical workspace changed since ChangeSet base revision")
        if not changeset.files:
            raise ChangeSetRejected("empty ChangeSet cannot be adopted")

        target = Path(mkdtemp(prefix="integration-", dir=self.workspace_root))
        shutil.rmtree(target)
        try:
            self._git(["worktree", "add", "--detach", str(target), current_revision])
            for change in changeset.files:
                relative = _normal_path(change.path)
                destination = target / relative
                if change.after_content is None:
                    if destination.exists():
                        destination.unlink()
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                if _hash(destination.read_bytes() if destination.is_file() else None) != change.before_hash:
                    raise IntegrationConflict(f"integration base changed for {relative}")
                destination.write_bytes(change.after_content)
            self._git(["add", "--", *(_normal_path(change.path) for change in changeset.files)], cwd=target)
            message = commit_message or f"Adopt ChangeSet from {changeset.owner_id}"
            if not message.strip():
                raise ValueError("commit_message must not be blank")
            self._git(["commit", "-m", message], cwd=target)
            commit = self._git(["rev-parse", "HEAD"], cwd=target)
            return AdoptionResult(commit=commit, revision=commit)
        except IntegrationConflict:
            raise
        except ChangeSetRejected:
            raise
        except (OSError, GitWorkspaceError) as exc:
            raise GitWorkspaceError(f"failed to adopt ChangeSet: {exc}") from exc
        finally:
            try:
                self._git(["worktree", "remove", "--force", str(target)])
            except GitWorkspaceError:
                shutil.rmtree(target, ignore_errors=True)
            else:
                shutil.rmtree(target, ignore_errors=True)

    def _owner_root(self, owner_id: str, owner_workspace: Path | str) -> Path:
        root = Path(owner_workspace).resolve()
        if self._owners.get(root) != owner_id or root.parent != self.workspace_root or not root.is_dir():
            raise ValueError("owner_workspace must be an adapter-created Git worktree for this owner")
        return root

    @staticmethod
    def _owner_name(owner_id: str) -> str:
        if not owner_id or "/" in owner_id or "\\" in owner_id or owner_id in {".", ".."}:
            raise ValueError("owner_id must be a simple non-empty name")
        return owner_id

    def _require_clean_canonical(self) -> None:
        if self._git(["status", "--porcelain", "--untracked-files=all"]):
            raise GitWorkspaceError("canonical Git checkout must be clean before creating a snapshot")

    def _git(self, args: list[str], *, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ["git", "-C", str(cwd or self.canonical_root), *args],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
            raise GitWorkspaceError(detail)
        return result.stdout.strip()

    def _git_bytes(self, args: list[str]) -> bytes:
        result = subprocess.run(
            ["git", "-C", str(self.canonical_root), *args],
            check=False,
            capture_output=True,
        )
        if result.returncode:
            raise GitWorkspaceError(result.stderr.decode(errors="replace").strip() or "unknown Git error")
        return result.stdout


def _worktree_files(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if relative.parts[0] == ".git":
            continue
        if path.is_symlink():
            raise ValueError(f"symlinks are not supported in Git owner workspaces: {path}")
        if path.is_file():
            files[_normal_path(relative.as_posix())] = path.read_bytes()
    return files


def _allows(path: str, region: ResourceRegion, base_revision: str) -> bool:
    if region.base_revision != base_revision:
        return False
    if region.resource_kind == "file":
        return _normal_path(region.resource_id) == path and region.selector_type == "whole_resource"
    if region.resource_kind == "directory" and region.selector_type == "path_prefix":
        prefix = _normal_path(str(region.selector_value)).rstrip("/")
        return path == prefix or path.startswith(prefix + "/")
    return False


__all__ = ["AdoptionResult", "GitWorkspaceAdapter", "GitWorkspaceError"]
