"""Local execution bridge between queued WorkerAttempts and a Provider adapter."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, Protocol

from valueroute.domain.models import ExecutionRequest, WorkerAttempt, WorkerAttemptStatus, WorkerAttemptTransitionConditions
from valueroute.domain.state_machine import transition_worker_attempt
from valueroute.execution.manager import ExecutionManager
from valueroute.execution.coordination import (
    ExecutionHandle,
    get_execution_handle,
    register_execution_handle,
    release_execution_handle,
)
from valueroute.observability.usage import UsageRecord
from valueroute.storage.interfaces import StateStore
from valueroute.storage.checkpoints import Checkpoint, CheckpointStore
from valueroute.execution.interfaces import ExecutionQueue
from valueroute.workspaces.interfaces import WorkspaceAdapter


class AsyncProvider(Protocol):
    """The minimal Provider contract required to execute a WorkerAttempt."""

    async def complete(
        self,
        *,
        task_id: str,
        input_text: str,
        reasoning_effort: str = "medium",
        retries: int = 0,
    ) -> Any: ...


UsageCallback = Callable[[UsageRecord], Awaitable[None] | None]


class WorkerRunner:
    """Claims one queued attempt, executes it locally, and records its terminal state."""

    def __init__(
        self,
        store: StateStore,
        provider: AsyncProvider,
        *,
        on_usage: UsageCallback | None = None,
        provider_timeout: float | None = 60.0,
        claim_ttl: timedelta = timedelta(seconds=60),
        checkpoint_store: CheckpointStore | None = None,
        cancel_grace_seconds: float = 10.0,
        cancel_grace: float | None = None,
        queue: ExecutionQueue | None = None,
        workspace_adapter: WorkspaceAdapter | None = None,
        execution: ExecutionManager | None = None,
    ):
        if provider_timeout is not None and provider_timeout <= 0:
            raise ValueError("provider_timeout must be positive or None")
        if cancel_grace is not None:
            cancel_grace_seconds = cancel_grace
        if cancel_grace_seconds < 0:
            raise ValueError("cancel_grace_seconds must not be negative")
        self.store = store
        self.provider = provider
        self.on_usage = on_usage
        self.provider_timeout = provider_timeout
        self.claim_ttl = claim_ttl
        self.checkpoint_store = checkpoint_store
        self.cancel_grace_seconds = cancel_grace_seconds
        self._manager = execution or ExecutionManager(
            store,
            queue=queue,
            workspace_adapter=workspace_adapter,
        )

    def get_execution_handle(self, attempt_id: str) -> ExecutionHandle | None:
        """Return the live handle for an attempt, if this runner owns it."""
        return get_execution_handle(self.store, attempt_id)

    def request_pause(self, attempt_id: str) -> bool:
        handle = self.get_execution_handle(attempt_id)
        if handle is None:
            return False
        handle.request_pause()
        return True

    def request_cancel(self, attempt_id: str) -> bool:
        handle = self.get_execution_handle(attempt_id)
        if handle is None:
            return False
        handle.request_cancel()
        return True

    async def run(
        self,
        attempt_id: str,
        *,
        task_id: str,
        input_text: str,
        reasoning_effort: str = "medium",
        retries: int = 0,
        claim: bool = True,
    ) -> WorkerAttempt:
        """Run one queued attempt and return its actual terminal WorkerAttempt state.

        Provider errors and timeouts are normal execution outcomes: both leave the
        attempt in ``failed``.  They are deliberately not converted into success.
        """
        self._persist_provider_request(
            attempt_id,
            ExecutionRequest(task_id=task_id, input_text=input_text, reasoning_effort=reasoning_effort, retries=retries),
        )
        claimed = self._manager.claim_attempt(attempt_id, ttl=self.claim_ttl) if claim else self._claimed_attempt(attempt_id)
        running = transition_worker_attempt(
            claimed,
            WorkerAttemptStatus.running,
            claimed.version,
            conditions=WorkerAttemptTransitionConditions(claim_token_valid=True),
        )
        self.store.attempts[attempt_id] = running
        self.store.commit({"type": "worker.started", "data": running.model_dump(mode="json") | {"session_id": self.store.attempt_session.get(attempt_id)}})
        self._checkpoint(running, safe_to_resume=True, next_step="retry provider call from the last provider boundary")

        handle = ExecutionHandle(attempt_id, cancel_grace_seconds=self.cancel_grace_seconds)
        register_execution_handle(self.store, handle)
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(attempt_id, claimed.claim_token, handle))

        try:
            self._sync_persisted_control(handle, attempt_id)
            if handle.cancel_requested:
                return self._cancelled(attempt_id)
            if handle.pause_requested:
                return self._paused(attempt_id)

            call = self.provider.complete(
                task_id=task_id,
                input_text=input_text,
                reasoning_effort=reasoning_effort,
                retries=retries,
                **self._handle_argument(handle),
                **self._workspace_argument(self.store.attempts[attempt_id]),
            )
            provider_task = asyncio.create_task(self._await_result(call))
            cancel_waiter = asyncio.create_task(handle.wait_for_cancel())
            handle._provider_task = provider_task
            try:
                done, _ = await asyncio.wait(
                    {provider_task, cancel_waiter},
                    timeout=self.provider_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                ) if self.provider_timeout is not None else await asyncio.wait(
                    {provider_task, cancel_waiter},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    provider_task.cancel()
                    await asyncio.gather(provider_task, return_exceptions=True)
                    raise asyncio.TimeoutError
                if cancel_waiter in done:
                    stopped = await self._cancel_provider(provider_task, task_id, handle)
                    return self._cancelled(attempt_id) if stopped else self._cancel_failed(attempt_id)
                result = await provider_task
            finally:
                if not cancel_waiter.done():
                    cancel_waiter.cancel()
                await asyncio.gather(cancel_waiter, return_exceptions=True)

            if self._is_cancel_requested(attempt_id, handle):
                return self._cancelled(attempt_id)
            usage = result.usage
            usage_callback = self.on_usage or self.store.record_usage
            if usage_callback is not None:
                callback_result = usage_callback(usage)
                if inspect.isawaitable(callback_result):
                    await callback_result
            if self._is_cancel_requested(attempt_id, handle):
                return self._cancelled(attempt_id)
            if self._is_pause_requested(attempt_id, handle):
                return self._paused(attempt_id)
        except Exception:
            if self._is_cancel_requested(attempt_id, handle):
                return self._cancelled(attempt_id)
            if self._is_pause_requested(attempt_id, handle):
                return self._terminal(attempt_id, WorkerAttemptStatus.failed)
            return self._terminal(attempt_id, WorkerAttemptStatus.failed)
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            handle.mark_done()
            release_execution_handle(self.store, handle)

        return self._terminal(attempt_id, WorkerAttemptStatus.succeeded)

    @staticmethod
    async def _await_result(value: Any) -> Any:
        return await value if inspect.isawaitable(value) else value

    async def _heartbeat_loop(self, attempt_id: str, token: str | None, handle: ExecutionHandle) -> None:
        """Keep the durable claim alive while a provider call is in flight."""
        if not token:
            return
        interval = max(0.01, self.claim_ttl.total_seconds() / 3)
        while not handle.done:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            if handle.done:
                return
            attempt = self.store.attempts.get(attempt_id)
            if attempt is None or attempt.status not in {WorkerAttemptStatus.claimed, WorkerAttemptStatus.running}:
                return
            try:
                self._manager.heartbeat(attempt_id, token, ttl=self.claim_ttl)
            except Exception:
                # Terminal transitions and external recovery can race the
                # heartbeat.  The durable state remains authoritative.
                return

    def _persist_provider_request(self, attempt_id: str, request: ExecutionRequest) -> None:
        attempt = self.store.attempts.get(attempt_id)
        if attempt is None:
            raise ValueError("worker attempt not found")
        if attempt.provider_request is not None:
            if attempt.provider_request != request:
                raise ValueError("provider request is immutable for an attempt")
            return
        updated = attempt.model_copy(update={"provider_request": request, "version": attempt.version + 1})
        self.store.commit({"type": "worker.request_bound", "data": updated.model_dump(mode="json") | {"session_id": self.store.attempt_session.get(attempt_id)}})
        self.store.attempts[attempt_id] = updated

    def _handle_argument(self, handle: ExecutionHandle) -> dict[str, Any]:
        """Pass the handle only to providers that explicitly accept it."""
        try:
            parameters = inspect.signature(self.provider.complete).parameters.values()
        except (TypeError, ValueError):
            return {}
        if "execution_handle" in {parameter.name for parameter in parameters} or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters
        ):
            return {"execution_handle": handle}
        return {}

    def _workspace_argument(self, attempt: WorkerAttempt) -> dict[str, Any]:
        binding = attempt.workspace
        if binding is None:
            return {}
        try:
            parameters = inspect.signature(self.provider.complete).parameters.values()
        except (TypeError, ValueError):
            return {}
        names = {parameter.name for parameter in parameters}
        accepts_kwargs = any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters)
        if "workspace_path" in names or accepts_kwargs:
            return {"workspace_path": binding.owner_workspace}
        return {}

    def _sync_persisted_control(self, handle: ExecutionHandle, attempt_id: str) -> None:
        status = self.store.attempts[attempt_id].status
        if status is WorkerAttemptStatus.cancel_requested:
            handle.request_cancel()
        elif status is WorkerAttemptStatus.pause_requested:
            handle.request_pause()

    def _is_cancel_requested(self, attempt_id: str, handle: ExecutionHandle) -> bool:
        return handle.cancel_requested or self.store.attempts[attempt_id].status is WorkerAttemptStatus.cancel_requested

    def _is_pause_requested(self, attempt_id: str, handle: ExecutionHandle) -> bool:
        return handle.pause_requested or self.store.attempts[attempt_id].status is WorkerAttemptStatus.pause_requested

    async def _cancel_provider(self, provider_task: asyncio.Task[Any], task_id: str, handle: ExecutionHandle) -> bool:
        cancel = getattr(self.provider, "cancel", None)
        if cancel is not None:
            try:
                result = cancel(task_id=task_id, execution_handle=handle)
            except TypeError:
                result = cancel(task_id=task_id)
            if inspect.isawaitable(result):
                result = await result
            if result is False:
                return False
        if provider_task.done():
            await asyncio.gather(provider_task, return_exceptions=True)
            return True
        try:
            await asyncio.wait_for(asyncio.shield(provider_task), timeout=handle.cancel_grace_seconds)
            return True
        except asyncio.TimeoutError:
            # Cancelling only the local asyncio task does not prove that a
            # remote provider stopped. Preserve that uncertainty as failed.
            provider_task.cancel()
            done, _ = await asyncio.wait({provider_task}, timeout=0)
            if done:
                await asyncio.gather(provider_task, return_exceptions=True)
            return False
        except asyncio.CancelledError:
            await asyncio.gather(provider_task, return_exceptions=True)
            return True

    def _paused(self, attempt_id: str) -> WorkerAttempt:
        attempt = self.store.attempts[attempt_id]
        if attempt.status is WorkerAttemptStatus.running:
            attempt = self._manager.request_control(attempt_id, "pause")
        if attempt.status is not WorkerAttemptStatus.pause_requested:
            return attempt
        attempt = self._checkpoint(
            attempt,
            safe_to_resume=True,
            next_step="resume provider call from the last provider boundary",
        )
        updated = transition_worker_attempt(
            attempt,
            WorkerAttemptStatus.paused,
            attempt.version,
            conditions=WorkerAttemptTransitionConditions(checkpoint_durable=True),
        )
        self.store.attempts[attempt_id] = updated
        self.store.commit({"type": "worker.paused", "data": updated.model_dump(mode="json") | {"session_id": self.store.attempt_session.get(attempt_id)}})
        self.store.commit({"type": "worker.checkpointed", "data": updated.model_dump(mode="json") | {"session_id": self.store.attempt_session.get(attempt_id)}})
        return updated

    def _cancelled(self, attempt_id: str) -> WorkerAttempt:
        attempt = self.store.attempts[attempt_id]
        if attempt.status is WorkerAttemptStatus.running:
            attempt = self._manager.request_control(attempt_id, "cancel")
        if attempt.status is not WorkerAttemptStatus.cancel_requested:
            return attempt
        updated = transition_worker_attempt(
            attempt,
            WorkerAttemptStatus.cancelled,
            attempt.version,
            conditions=WorkerAttemptTransitionConditions(execution_stopped=True),
        )
        self.store.attempts[attempt_id] = updated
        self.store.commit({"type": "worker.stopped", "data": updated.model_dump(mode="json") | {"session_id": self.store.attempt_session.get(attempt_id)}})
        return updated

    def _cancel_failed(self, attempt_id: str) -> WorkerAttempt:
        """Record a failed cancellation without claiming remote termination."""
        attempt = self.store.attempts[attempt_id]
        if attempt.status is not WorkerAttemptStatus.cancel_requested:
            return attempt
        updated = transition_worker_attempt(
            attempt,
            WorkerAttemptStatus.failed,
            attempt.version,
            conditions=WorkerAttemptTransitionConditions(execution_stopped=False),
        )
        self.store.attempts[attempt_id] = updated
        self.store.commit({"type": "worker.cancel_failed", "data": updated.model_dump(mode="json") | {"session_id": self.store.attempt_session.get(attempt_id)}})
        return updated

    def _checkpoint(self, attempt: WorkerAttempt, *, safe_to_resume: bool, next_step: str, failures: list[str] | None = None) -> WorkerAttempt:
        if self.checkpoint_store is None:
            return attempt
        checkpoint = Checkpoint(
            id=f"cp_{attempt.id}_{attempt.version + 1}",
            boundary_version=attempt.version,
            owner_version=1,
            confirmed_facts=[f"worker attempt {attempt.id} reached provider boundary"],
            recent_failures=failures or [],
            next_step=next_step,
            safe_to_resume=safe_to_resume,
        )
        self.checkpoint_store.save(checkpoint)
        updated = attempt.model_copy(update={"checkpoint_id": checkpoint.id, "checkpoint_safe_to_resume": safe_to_resume, "version": attempt.version + 1})
        self.store.attempts[attempt.id] = updated
        self.store.commit({"type": "worker.checkpointed", "data": updated.model_dump(mode="json") | {"session_id": self.store.attempt_session.get(attempt.id)}})
        return updated

    def _terminal(self, attempt_id: str, status: WorkerAttemptStatus) -> WorkerAttempt:
        attempt = self.store.attempts[attempt_id]
        attempt = self._checkpoint(attempt, safe_to_resume=status is WorkerAttemptStatus.succeeded, next_step="stop" if status is WorkerAttemptStatus.succeeded else "inspect failure before retry", failures=[] if status is WorkerAttemptStatus.succeeded else [status.value])
        updated = transition_worker_attempt(attempt, status, attempt.version)
        self.store.attempts[attempt_id] = updated
        self.store.commit({"type": "worker.stopped", "data": updated.model_dump(mode="json") | {"session_id": self.store.attempt_session.get(attempt_id)}})
        return self._manager.ack_terminal(attempt_id, status=status)

    def _claimed_attempt(self, attempt_id: str) -> WorkerAttempt:
        attempt = self.store.attempts.get(attempt_id)
        if attempt is None or attempt.status is not WorkerAttemptStatus.claimed:
            raise ValueError("attempt must be claimed before claim=False execution")
        return attempt
