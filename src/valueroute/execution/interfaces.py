"""Replaceable execution-queue contract."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ExecutionQueue(Protocol):
    def enqueue(self, attempt: Any, *, session_id: str | None = None) -> Any: ...

    def claim_next(self, *, now: datetime | None = None, ttl: timedelta = timedelta(seconds=60)) -> Any | None: ...

    def claim(self, attempt_id: str, *, now: datetime | None = None, ttl: timedelta = timedelta(seconds=60)) -> Any: ...

    def requeue(self, attempt_id: str, *, checkpoint_store: Any | None = None) -> Any: ...

    def ack_terminal(self, attempt_id: str, *, status: Any | None = None) -> Any: ...

    def queued(self) -> list[Any]: ...

    def recover(self, attempt_id: str, checkpoint_store: Any) -> Any: ...


__all__ = ["ExecutionQueue"]
