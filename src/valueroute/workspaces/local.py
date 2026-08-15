"""A fail-closed local-directory WorkspaceAdapter.

Workers only receive copied owner directories.  A ChangeSet captures bytes from
that directory, so integration never reads a worker directory a second time.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import unified_diff
from hashlib import sha256
from pathlib import Path, PurePosixPath
from tempfile import mkdtemp
from threading import RLock

from valueroute.domain.models import ResourceRegion, WriterLease


def _hash(data: bytes | None) -> str | None:
    return None if data is None else sha256(data).hexdigest()


def _normal_path(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("workspace paths must be relative and cannot escape the workspace")
    result = path.as_posix()
    if result in ("", "."):
        raise ValueError("workspace path must name a file or directory")
    return result


def _files(root: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"symlinks are not supported in local workspaces: {path}")
        if path.is_file():
            result[_normal_path(path.relative_to(root).as_posix())] = path.read_bytes()
    return result


def _revision(files: dict[str, bytes]) -> str:
    digest = sha256()
    for path in sorted(files):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256(files[path]).digest())
        digest.update(b"\0")
    return digest.hexdigest()


def _diff(path: str, before: bytes | None, after: bytes | None) -> str:
    def lines(data: bytes | None) -> list[str]:
        return (data or b"").decode("utf-8", errors="replace").splitlines(keepends=True)

    return "".join(unified_diff(lines(before), lines(after), fromfile=f"a/{path}", tofile=f"b/{path}"))


@dataclass(frozen=True)
class WorkspaceSnapshot:
    revision: str
    files: dict[str, bytes]


@dataclass(frozen=True)
class FileChange:
    path: str
    before_hash: str | None
    after_hash: str | None
    diff: str
    after_content: bytes | None


@dataclass(frozen=True)
class ChangeSet:
    owner_id: str
    base_revision: str
    files: tuple[FileChange, ...]


class ChangeSetRejected(Exception):
    code = "write_scope_violation"


class IntegrationConflict(Exception):
    code = "integration_conflict"


class CanonicalWriteRejected(Exception):
    code = "canonical_write_forbidden"


@dataclass(frozen=True)
class OwnerWorkspace:
    """A worker-visible copy and the canonical revision it was copied from."""

    owner_id: str
    path: Path
    base_revision: str
    adapter_mode: str = "copy"


class LocalWorkspaceAdapter:
    """Copies a local canonical directory and integrates validated ChangeSets."""

    def __init__(self, canonical_root: Path | str, workspace_root: Path | str):
        self.canonical_root = Path(canonical_root).resolve()
        self.workspace_root = Path(workspace_root).resolve()
        if not self.canonical_root.is_dir():
            raise ValueError("canonical_root must be an existing directory")
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self._integration_lock = RLock()
        self._git_root = self._discover_git_root()

    def snapshot(self) -> WorkspaceSnapshot:
        if self._git_root is not None:
            files = _files(self.canonical_root)
            return WorkspaceSnapshot(revision=self._git_revision(self.canonical_root), files=files)
        files = _files(self.canonical_root)
        return WorkspaceSnapshot(revision=_revision(files), files=files)

    def create_owner_workspace(self, owner_id: str, snapshot: WorkspaceSnapshot) -> Path:
        if not owner_id or "/" in owner_id or "\\" in owner_id:
            raise ValueError("owner_id must be a simple non-empty name")
        if self.snapshot().revision != snapshot.revision:
            raise IntegrationConflict("workspace snapshot is no longer canonical")
        if self._git_root is None:
            target = Path(mkdtemp(prefix=f"{owner_id}-", dir=self.workspace_root))
            try:
                for relative, content in snapshot.files.items():
                    path = target / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
                return target
            except BaseException:
                shutil.rmtree(target, ignore_errors=True)
                raise
        target = Path(mkdtemp(prefix=f"{owner_id}-", dir=self.workspace_root))
        try:
            self._run_git(
                "worktree",
                "add",
                "--detach",
                str(target),
                snapshot.revision,
                cwd=self.canonical_root,
            )
            return target
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise

    def bind_owner_workspace(self, owner_id: str, *, base_revision: str | None = None) -> OwnerWorkspace:
        """Create a worker copy, failing if the requested canonical revision is stale."""
        snapshot = self.snapshot()
        if base_revision is not None and snapshot.revision != base_revision:
            raise IntegrationConflict("canonical workspace revision does not match task base revision")
        path = self.create_owner_workspace(owner_id, snapshot)
        mode = "git-worktree" if self._git_root is not None else "copy"
        return OwnerWorkspace(owner_id=owner_id, path=path, base_revision=snapshot.revision, adapter_mode=mode)

    def write_file(self, owner_workspace: Path | str, relative_path: str, content: bytes) -> None:
        """Write only inside an adapter-created owner workspace.

        Workers receive this boundary instead of the canonical directory.  The
        explicit canonical check keeps accidental use of the adapter as a
        direct canonical writer fail-closed.
        """
        root = self._owner_root(owner_workspace)
        path = root / _normal_path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def create_changeset(self, owner_id: str, owner_workspace: Path | str, snapshot: WorkspaceSnapshot) -> ChangeSet:
        root = self._owner_root(owner_workspace)
        current = _files(root)
        changes = tuple(
            FileChange(
                path=path,
                before_hash=_hash(snapshot.files.get(path)),
                after_hash=_hash(current.get(path)),
                diff=self._diff_for_path(root, snapshot.revision, path, snapshot.files.get(path), current.get(path)),
                after_content=current.get(path),
            )
            for path in sorted(set(snapshot.files) | set(current))
            if snapshot.files.get(path) != current.get(path)
        )
        return ChangeSet(owner_id=owner_id, base_revision=snapshot.revision, files=changes)

    def cleanup_owner_workspace(self, owner_workspace: Path | str) -> None:
        root = self._owner_root(owner_workspace)
        if self._git_root is None:
            shutil.rmtree(root)
            return
        try:
            self._run_git("worktree", "remove", "--force", str(root), cwd=self.canonical_root)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def _owner_root(self, owner_workspace: Path | str) -> Path:
        root = Path(owner_workspace).resolve()
        if root == self.canonical_root:
            raise CanonicalWriteRejected("canonical workspace is not a worker workspace")
        if root.parent != self.workspace_root or not root.is_dir():
            raise ValueError("owner_workspace must be an adapter-created workspace")
        return root

    def _discover_git_root(self) -> Path | None:
        try:
            result = self._run_git("rev-parse", "--show-toplevel", cwd=self.canonical_root)
        except Exception:
            return None
        git_root = Path(result).resolve()
        return git_root if git_root == self.canonical_root else None

    def _git_revision(self, cwd: Path) -> str:
        return self._run_git("rev-parse", "HEAD", cwd=cwd)

    def _diff_for_path(self, root: Path, base_revision: str, path: str, before: bytes | None, after: bytes | None) -> str:
        if before is None or after is None:
            return _diff(path, before, after)
        if self._git_root is None:
            return _diff(path, before, after)
        try:
            return self._run_git("diff", "--binary", "--no-ext-diff", base_revision, "--", path, cwd=root)
        except Exception:
            return _diff(path, before, after)

    def _run_git(self, *args: str, cwd: Path) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    def validate_changeset(self, changeset: ChangeSet, leases: Iterable[WriterLease]) -> None:
        active = [lease for lease in leases if lease.status == "active" and lease.owner_agent_id == changeset.owner_id]
        for change in changeset.files:
            if not any(_allows(change.path, lease.region, changeset.base_revision) for lease in active):
                raise ChangeSetRejected(f"write_scope_violation: {change.path}")

    def integrate(self, changeset: ChangeSet, leases: Iterable[WriterLease]) -> WorkspaceSnapshot:
        """Validate, preflight, and replace the canonical directory only on success."""
        with self._integration_lock:
            self.validate_changeset(changeset, leases)
            current = self.snapshot()
            if current.revision != changeset.base_revision:
                # A parallel worker may have integrated a disjoint change from
                # the same base. Re-map only when every changed path still has
                # the exact bytes observed by this ChangeSet; a changed path
                # is an overlap/conflict and remains fail-closed.
                for change in changeset.files:
                    if _hash(current.files.get(change.path)) != change.before_hash:
                        raise IntegrationConflict("canonical workspace changed since ChangeSet base revision in a ChangeSet path")

            parent = self.canonical_root.parent
            staging = Path(mkdtemp(prefix=f".{self.canonical_root.name}.integration-", dir=parent))
            backup = parent / f".{self.canonical_root.name}.backup-{os.urandom(8).hex()}"
            try:
                shutil.rmtree(staging)
                shutil.copytree(self.canonical_root, staging, symlinks=False)
                for change in changeset.files:
                    target = staging / change.path
                    if change.after_content is None:
                        target.unlink(missing_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(change.after_content)
                os.replace(self.canonical_root, backup)
                try:
                    os.replace(staging, self.canonical_root)
                except BaseException:
                    os.replace(backup, self.canonical_root)
                    raise
                shutil.rmtree(backup, ignore_errors=True)
                return self.snapshot()
            finally:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)


def _allows(path: str, region: ResourceRegion, base_revision: str) -> bool:
    if region.base_revision != base_revision:
        return False
    if region.resource_kind == "file":
        return _normal_path(region.resource_id) == path and region.selector_type == "whole_resource"
    if region.resource_kind == "directory" and region.selector_type == "path_prefix":
        prefix = _normal_path(str(region.selector_value)).rstrip("/")
        return path == prefix or path.startswith(prefix + "/")
    return False
