"""Bounded local execution supervisor for the journal-backed queue."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from valueroute.domain.models import ExecutionRequest, WorkerAttempt
from valueroute.execution.runner import AsyncProvider, WorkerRunner
from valueroute.storage.interfaces import StateStore
from valueroute.execution.interfaces import ExecutionQueue
from valueroute.workspaces.interfaces import WorkspaceAdapter


class ExecutionSupervisor:
    """Consume queued attempts with durable claim-before-execution semantics.

    This is intentionally a local supervisor: it owns no new persistence and
    can be restarted against the same Store. Safe queued attempts are then
    consumed again; attempts already claimed at process death are reclaimed by
    Store startup recovery when a checkpoint permits it.
    """

    def __init__(
        self,
        store: StateStore,
        provider: AsyncProvider,
        *,
        requests: dict[str, ExecutionRequest] | None = None,
        max_concurrency: int = 5,
        claim_ttl: timedelta = timedelta(seconds=60),
        runner_kwargs: dict[str, Any] | None = None,
        queue: ExecutionQueue | None = None,
        workspace_adapter: WorkspaceAdapter | None = None,
    ) -> None:
        if not 1 <= max_concurrency <= 5:
            raise ValueError("max_concurrency must be between 1 and 5")
        self.store = store
        self.requests = requests if requests is not None else {}
        self.max_concurrency = max_concurrency
        self.claim_ttl = claim_ttl
        runner_options = dict(runner_kwargs or {})
        runner_options.setdefault("claim_ttl", claim_ttl)
        runner_options.setdefault("queue", queue)
        runner_options.setdefault("workspace_adapter", workspace_adapter)
        self.runner = WorkerRunner(store, provider, **runner_options)
        if self.runner.checkpoint_store is not None:
            # Reclaim only attempts with a durable safe boundary.  This is
            # deliberately performed before the first queue scan so a new
            # supervisor can continue work without an in-memory registration.
            self.store.reclaim_attempts(self.runner.checkpoint_store)
        self._claim_lock = asyncio.Lock()

    def register(self, attempt_id: str, request: ExecutionRequest) -> None:
        attempt = self.store.attempts.get(attempt_id)
        if attempt is None:
            raise ValueError("worker attempt not found")
        if attempt.provider_request is not None and attempt.provider_request != request:
            raise ValueError("provider request is immutable for an attempt")
        if attempt.provider_request is None:
            updated = attempt.model_copy(update={"provider_request": request, "version": attempt.version + 1})
            self.store.commit({"type": "worker.request_bound", "data": updated.model_dump(mode="json") | {"session_id": self.store.attempt_session.get(attempt_id)}})
            self.store.attempts[attempt_id] = updated
        self.requests[attempt_id] = request

    async def run_once(self) -> WorkerAttempt | None:
        """Claim and execute the next queued attempt with a registered request."""
        async with self._claim_lock:
            candidate = next(
                (item for item in self.runner._manager.queue.queued() if self._request_for(item) is not None),
                None,
            )
            if candidate is None:
                return None
            claimed = self.runner._manager.claim_attempt(candidate.id, ttl=self.claim_ttl)
        request = self._request_for(claimed)
        assert request is not None
        return await self.runner.run(
            claimed.id,
            task_id=request.task_id,
            input_text=request.input_text,
            reasoning_effort=request.reasoning_effort,
            retries=request.retries,
            claim=False,
        )

    def _request_for(self, attempt: WorkerAttempt) -> ExecutionRequest | None:
        if attempt.provider_request is not None:
            return attempt.provider_request
        request = self.requests.get(attempt.id)
        if request is not None:
            return request
        # Reconstruct the minimal provider input from the durable child
        # boundary after a supervisor restart. Hosts may still register a
        # richer request when the objective needs additional context.
        child = self.store.children.get(attempt.child_task_id)
        if child is None:
            return None
        return ExecutionRequest(task_id=child.id, input_text=child.objective)

    async def run_until_idle(self) -> list[WorkerAttempt]:
        """Drain registered work, bounded by the configured concurrency."""
        results: list[WorkerAttempt] = []
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def drain_one() -> WorkerAttempt | None:
            async with semaphore:
                return await self.run_once()

        while True:
            batch = await asyncio.gather(*(drain_one() for _ in range(self.max_concurrency)))
            completed = [item for item in batch if item is not None]
            if not completed:
                return results
            results.extend(completed)

    async def run_forever(self, *, stop_event: asyncio.Event | None = None, idle_sleep_seconds: float = 0.05) -> None:
        """Continuously consume durable work until the host requests shutdown."""
        if idle_sleep_seconds <= 0:
            raise ValueError("idle_sleep_seconds must be positive")
        stop_event = stop_event or asyncio.Event()
        while not stop_event.is_set():
            result = await self.run_once()
            if result is not None:
                continue
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=idle_sleep_seconds)
            except asyncio.TimeoutError:
                pass


__all__ = ["ExecutionRequest", "ExecutionSupervisor"]
