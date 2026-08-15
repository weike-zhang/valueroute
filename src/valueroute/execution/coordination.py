"""In-process coordination primitives for a durable WorkerAttempt.

The journal remains the source of truth for the request state.  The handle is
only the local wake-up path for a runner that is currently executing an
attempt; a runner that is restarted can observe the persisted state instead.
"""

from __future__ import annotations

import asyncio
from typing import Any


class ExecutionHandle:
    """A small, cooperative control handle for one provider invocation."""

    def __init__(self, attempt_id: str, *, cancel_grace_seconds: float = 10.0):
        if cancel_grace_seconds < 0:
            raise ValueError("cancel_grace_seconds must not be negative")
        self.attempt_id = attempt_id
        self.cancel_grace_seconds = cancel_grace_seconds
        self._pause_requested = asyncio.Event()
        self._cancel_requested = asyncio.Event()
        self._done = asyncio.Event()

    @property
    def pause_requested(self) -> bool:
        return self._pause_requested.is_set()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_requested.is_set()

    @property
    def done(self) -> bool:
        return self._done.is_set()

    @property
    def pause_event(self) -> asyncio.Event:
        return self._pause_requested

    @property
    def cancel_event(self) -> asyncio.Event:
        return self._cancel_requested

    def request_pause(self) -> None:
        if not self.done and not self.cancel_requested:
            self._pause_requested.set()

    def request_cancel(self) -> None:
        if not self.done:
            self._cancel_requested.set()

    def mark_done(self) -> None:
        self._done.set()

    async def wait_for_cancel(self) -> None:
        await self._cancel_requested.wait()


def _handles(store: Any) -> dict[str, ExecutionHandle]:
    handles = getattr(store, "_execution_handles", None)
    if handles is None:
        handles = {}
        setattr(store, "_execution_handles", handles)
    return handles


def register_execution_handle(store: Any, handle: ExecutionHandle) -> None:
    _handles(store)[handle.attempt_id] = handle


def release_execution_handle(store: Any, handle: ExecutionHandle) -> None:
    handles = _handles(store)
    if handles.get(handle.attempt_id) is handle:
        handles.pop(handle.attempt_id, None)


def get_execution_handle(store: Any, attempt_id: str) -> ExecutionHandle | None:
    return _handles(store).get(attempt_id)


def request_execution_control(store: Any, attempt_id: str, action: str) -> bool:
    """Wake a local runner after a request was durably recorded.

    Returning ``False`` is expected when the attempt is not running in this
    process.  Its persisted request is still valid for a later runner.
    """

    handle = get_execution_handle(store, attempt_id)
    if handle is None:
        return False
    if action == "pause":
        handle.request_pause()
    elif action == "cancel":
        handle.request_cancel()
    else:
        raise ValueError(f"unsupported execution control action: {action}")
    return True


__all__ = [
    "ExecutionHandle",
    "get_execution_handle",
    "register_execution_handle",
    "release_execution_handle",
    "request_execution_control",
]
