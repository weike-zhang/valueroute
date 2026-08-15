"""Local isolated workspaces and ChangeSet integration primitives."""

from .local import (
    ChangeSet,
    ChangeSetRejected,
    CanonicalWriteRejected,
    FileChange,
    IntegrationConflict,
    LocalWorkspaceAdapter,
    OwnerWorkspace,
    WorkspaceSnapshot,
)
from .git import AdoptionResult, GitWorkspaceAdapter, GitWorkspaceError

__all__ = [
    "ChangeSet",
    "ChangeSetRejected",
    "AdoptionResult",
    "CanonicalWriteRejected",
    "FileChange",
    "IntegrationConflict",
    "LocalWorkspaceAdapter",
    "OwnerWorkspace",
    "WorkspaceSnapshot",
    "GitWorkspaceAdapter",
    "GitWorkspaceError",
]
