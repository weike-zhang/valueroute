"""Workspace adapter contract for isolated owners and atomic integration."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class WorkspaceAdapter(Protocol):
    def bind_owner_workspace(self, owner_id: str, *, base_revision: str | None = None) -> Any: ...

    def snapshot(self) -> Any: ...

    def create_changeset(self, owner_id: str, owner_workspace: Any, snapshot: Any) -> Any: ...

    def cleanup_owner_workspace(self, owner_workspace: Any) -> None: ...

    def integrate(self, changeset: Any, leases: Any) -> Any: ...

    def cleanup_owner_workspace(self, owner_workspace: Any) -> None: ...

    def validate_changeset(self, changeset: Any, leases: Any) -> None: ...


@runtime_checkable
class ExplicitChangeSetAdopter(Protocol):
    """Optional host-only boundary for adapters that can create revisions."""

    def adopt_changeset(self, changeset: Any, leases: Any, *, commit_message: str | None = None) -> Any: ...


__all__ = ["ExplicitChangeSetAdopter", "WorkspaceAdapter"]
