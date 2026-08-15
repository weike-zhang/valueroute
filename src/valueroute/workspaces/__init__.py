"""Local isolated workspaces and ChangeSet integration primitives."""

from .git import AdoptionResult, GitWorkspaceAdapter, GitWorkspaceError
from .local import (
    CanonicalWriteRejected,
    ChangeSet,
    ChangeSetRejected,
    FileChange,
    IntegrationConflict,
    LocalWorkspaceAdapter,
    OwnerWorkspace,
    WorkspaceSnapshot,
)

__all__ = [
    "AdoptionResult",
    "CanonicalWriteRejected",
    "ChangeSet",
    "ChangeSetRejected",
    "FileChange",
    "GitWorkspaceAdapter",
    "GitWorkspaceError",
    "IntegrationConflict",
    "LocalWorkspaceAdapter",
    "OwnerWorkspace",
    "WorkspaceSnapshot",
]
