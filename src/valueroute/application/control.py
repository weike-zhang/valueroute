from __future__ import annotations

from typing import Literal

from valueroute.application.service import DomainError
from valueroute.domain.models import ParentTask, TaskStatus, new_id
from valueroute.domain.state_machine import StateTransitionError, transition_task
from valueroute.execution.interfaces import ExecutionQueue
from valueroute.execution.manager import ExecutionManager
from valueroute.storage.checkpoints import Checkpoint, CheckpointError, CheckpointStore
from valueroute.storage.interfaces import StateStore
from valueroute.workspaces.interfaces import WorkspaceAdapter


class ControlService:
    def __init__(
        self,
        store: StateStore,
        checkpoint_store: CheckpointStore | None = None,
        *,
        execution: ExecutionManager | None = None,
        queue: ExecutionQueue | None = None,
        workspace_adapter: WorkspaceAdapter | None = None,
    ):
        self.store = store
        self.checkpoint_store = checkpoint_store
        self.execution = execution or ExecutionManager(
            store,
            queue=queue,
            workspace_adapter=workspace_adapter,
        )

    def _request_worker_control(self, task: ParentTask, action: str) -> None:
        """Propagate a parent control request to its currently running workers."""
        child_ids = set(task.child_task_ids)
        for attempt in list(self.store.attempts.values()):
            if attempt.child_task_id in child_ids:
                self.execution.request_control(attempt.id, action)

    def transition(self, task_id: str, action: Literal["execute", "pause", "resume", "cancel"], expected_version: int, reason: str | None = None, idem: tuple[str, str, str] | None = None, payload: dict | None = None) -> ParentTask:
        if idem is not None and payload is not None:
            previous = self.store.check_idempotency(idem, payload)
            if previous:
                return ParentTask.model_validate(previous["event"]["data"])
        task = self.store.tasks.get(task_id)
        if not task:
            raise DomainError("not_found", "task not found", 404)
        if task.version != expected_version:
            raise DomainError("version_conflict", "task version has changed")
        session = self.store.sessions[task.controller_session_id]
        if action == "execute":
            if not session.active_controller_epoch_id:
                raise DomainError("controller_not_registered", "宿主必须先登记活动主控")
            try:
                current = task
                if current.status == TaskStatus.draft:
                    current = transition_task(current, TaskStatus.planned, current.version)
                if current.status == TaskStatus.planned:
                    current = transition_task(current, TaskStatus.queued, current.version)
                if current.status == TaskStatus.queued:
                    current = transition_task(current, TaskStatus.running, current.version)
                elif current.status == TaskStatus.paused:
                    current = transition_task(current, TaskStatus.queued, current.version)
                    current = transition_task(current, TaskStatus.running, current.version)
                else:
                    raise StateTransitionError(f"cannot execute task in {current.status}")
            except StateTransitionError as error:
                raise DomainError("invalid_transition", str(error)) from error
            updated = current
            target = TaskStatus.running
        elif action == "pause":
            try:
                updated = transition_task(task, TaskStatus.pause_requested, task.version)
                self._request_worker_control(task, "pause")
                updated = transition_task(updated, TaskStatus.paused, updated.version)
            except StateTransitionError as error:
                raise DomainError("invalid_transition", str(error)) from error
            target = TaskStatus.paused
        elif action == "resume":
            if self.checkpoint_store is not None:
                if not task.latest_checkpoint_id:
                    raise DomainError("not_resumable", "task has no recovery checkpoint")
                try:
                    checkpoint = self.checkpoint_store.load(task.latest_checkpoint_id)
                except CheckpointError as error:
                    raise DomainError("not_resumable", "recovery checkpoint is unavailable") from error
                if not checkpoint.safe_to_resume:
                    raise DomainError("not_resumable", "recovery checkpoint is not safe to resume")
            try:
                # A parent resume must make paused child attempts executable
                # again; changing only the parent state would leave the
                # durable queue empty and silently strand the work.
                for attempt in list(self.store.attempts.values()):
                    if attempt.child_task_id in set(task.child_task_ids) and attempt.status.value == "paused":
                        self.execution.requeue_attempt(attempt.id, checkpoint_store=self.checkpoint_store)
                updated = transition_task(task, TaskStatus.queued, task.version)
                updated = transition_task(updated, TaskStatus.running, updated.version)
            except StateTransitionError as error:
                raise DomainError("not_resumable", str(error)) from error
            target = TaskStatus.running
        else:
            try:
                updated = task
                if updated.status == TaskStatus.running:
                    updated = transition_task(updated, TaskStatus.cancel_requested, updated.version)
                    self._request_worker_control(task, "cancel")
                updated = transition_task(updated, TaskStatus.cancelled, updated.version)
            except StateTransitionError as error:
                raise DomainError("invalid_transition", str(error)) from error
            target = TaskStatus.cancelled
        if self.checkpoint_store is not None:
            checkpoint_id = new_id("cp")
            checkpoint = Checkpoint(
                id=checkpoint_id,
                boundary_version=updated.version,
                owner_version=1,
                confirmed_facts=[f"task transitioned to {target.value}"],
                recent_failures=[] if target != TaskStatus.failed else [reason or "task failed"],
                next_step="resume from this checkpoint" if target == TaskStatus.paused else "stop",
                safe_to_resume=target == TaskStatus.paused,
            )
            self.checkpoint_store.save(checkpoint)
            updated = updated.model_copy(update={"latest_checkpoint_id": checkpoint_id})
        self.store.tasks[task.id] = updated
        self.store.commit({"type": f"task.{target.value}", "data": {"task_id": task.id, "reason": reason, "status": target.value}})
        self.store.commit({"type": "task.updated", "data": updated.model_dump(mode="json")}, key=idem, payload=payload)
        return updated
