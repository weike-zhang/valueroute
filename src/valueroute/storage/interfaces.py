"""Persistence contracts used by the ValueRoute core.

The v0 local adapters are intentionally file-backed, but these contracts keep
the domain/application layer independent from their JSONL representation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class StateStore(Protocol):
    """Synchronous projection/persistence boundary used by the core.

    The first local implementation is an in-process projection backed by a
    journal.  Keeping the projection members in the contract is deliberate:
    application services need transactional reads for validation, while an
    alternative adapter can provide the same views without inheriting from the
    local ``Store`` class.
    """

    sessions: MutableMapping[str, Any]
    epochs: MutableMapping[str, Any]
    tasks: MutableMapping[str, Any]
    plans: MutableMapping[str, Any]
    leases: MutableMapping[str, Any]
    attempts: MutableMapping[str, Any]
    attempt_session: MutableMapping[str, str]
    terminal_acks: set[str]
    integration_attempts: MutableMapping[str, Any]
    usage: MutableMapping[str, list[Any]]
    approvals: MutableMapping[str, Any]
    approval_task: MutableMapping[str, str]
    children: MutableMapping[str, Any]
    child_ref_ids: MutableMapping[tuple[str, str], str]
    assignments: MutableMapping[str, Any]
    reviews: MutableMapping[str, Any]
    verifications: MutableMapping[str, Any]

    def commit(
        self,
        event: dict[str, Any],
        *,
        key: tuple[str, str, str] | None = None,
        payload: Any = None,
    ) -> None: ...

    def commit_frame(
        self,
        events: Iterable[dict[str, Any]],
        *,
        expected_versions: Mapping[str, int] | None = None,
        key: tuple[str, str, str] | None = None,
        payload: Any = None,
    ) -> None: ...

    def check_idempotency(self, key: tuple[str, str, str] | None, payload: Any) -> dict[str, Any] | None: ...

    def require_version(self, actual: int, expected: int) -> None: ...

    def record_integration_attempt(self, attempt: Any, event_type: str) -> Any: ...

    def latest_integration_attempt(self, client_ref: str, parent_task_id: str | None = None) -> Any | None: ...

    @staticmethod
    def integration_result(attempt: Any) -> dict[str, Any]: ...

    def record_usage(self, usage: Any) -> Any: ...

    def snapshot(self) -> Any: ...

    def rebuild(self) -> Any: ...

    def compact(self) -> Any: ...


@runtime_checkable
class ArtifactStore(Protocol):
    def put(self, content: bytes, *, media_type: str, data_classification: str) -> Any: ...

    def get(self, reference: Any) -> bytes: ...

    def verify(self, reference: Any) -> Any: ...


@runtime_checkable
class CheckpointStore(Protocol):
    """Durable recovery boundary consumed by application/execution services."""

    def save(self, checkpoint: Any) -> None: ...
    def load(self, checkpoint_id: str) -> Any: ...
    def list_ids(self) -> list[str]: ...
    def list_valid(self) -> list[Any]: ...


__all__ = ["ArtifactStore", "CheckpointStore", "StateStore"]
